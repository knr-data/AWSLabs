from __future__  import print_function
import boto3
import os, sys
import json
from neptune_python_utils.gremlin_utils import GremlinUtils
from neptune_python_utils.endpoints import Endpoints
from gremlin_python.process.graph_traversal import __
from gremlin_python.process.traversal import Column
from io import BytesIO, StringIO
from datetime import datetime as dt
import numpy as np
import pandas as pd
import time
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_FEATURE_NODE = int(os.environ['MAX_FEATURE_NODE'])
CLUSTER_ENDPOINT = os.environ['CLUSTER_ENDPOINT']
CLUSTER_PORT = os.environ['CLUSTER_PORT']
CLUSTER_REGION = os.environ['CLUSTER_REGION']
ENDPOINT_NAME = os.environ['ENDPOINT_NAME']
MODEL_BTW = float(os.environ['MODEL_BTW'])
QUEUE_URL = os.environ['QUEUE_URL']

transactions_id_cols = os.environ['TRANSACTION_ID_COLS']
identities_cols = os.environ['IDENTITIES_COLS']
neighbor_cols = os.environ['NEIGHBOR_COLS']
dummied_col = os.environ['DUMMIED_COL']

sqs = boto3.client('sqs')
runtime = boto3.client('runtime.sagemaker')

endpoints = Endpoints(neptune_endpoint = CLUSTER_ENDPOINT, neptune_port = CLUSTER_PORT, region_name = CLUSTER_REGION)

def load_data_from_event(input_event, transactions_id_cols, identities_cols, neighbor_cols, dummied_col):
    """Load and transform event data into correct format for next step subgraph loading and model inference input. 
        input event keys should come from related dataset.]
    
    Example:
    >>> load_data_from_event(event = {"TransactionID":"3163166", "V1":1, ...}, 'card1,card2,,...', 'id_01,id_02,,...', 'C1,C2,C3,C4,C5,C6,...', 'M2_T,M3_F,M3_T,...')
    """
    TRANSACTION_ID = 'TransactionID'
    input_event[TRANSACTION_ID] = f't-{input_event[TRANSACTION_ID]}'
    input_event = pd.DataFrame.from_dict(input_event, orient='index').transpose()

    new_id_cols = transactions_id_cols.split(',') + identities_cols.split(',') 
    dummied_col = dummied_col.split(',')
    neighbor_cols = neighbor_cols.split(',')

    target_id = input_event[TRANSACTION_ID].iloc[0]
    
    for dummy in dummied_col:
        col_name = dummy[:2]
        if input_event[col_name].iloc[0] == dummy[3:]:
            input_event[dummy] = 1.0
        else:
            input_event[dummy] = 0.0
            
    trans_dict = [input_event[neighbor_cols+dummied_col].iloc[0].fillna(0.0).to_dict()]
    identity_dict = [input_event[new_id_cols].iloc[0].fillna(0.0).to_dict()]
    return trans_dict, identity_dict, target_id

class GraphModelClient:
    def __init__(self, endpoint):
        self.gremlin_utils = GremlinUtils(endpoint)
        GremlinUtils.init_statics(globals())

    def insert_new_transaction_vertex_and_edge(self, tr_dict, connectted_node_dict, target_id, vertex_type = 'Transaction'):
        """Load transaction data, insert transaction object and related domain objects into GraphDB as vertex,
        with their properties as values, and insert their relation as edges.
            
        Example:
        >>> insert_new_transaction_vertex_and_edge(tr_dict, connectted_node_dict, target_id, vertex_type = 'Transaction')
        """
        def insert_attr(graph_conn, attr_val_dict, target_id, node_id, vertex_type): 

            if (not g.V().has(id,node_id).hasNext()):
                logger.info(f'Insert_Vertex: {node_id}.')
                g.inject(attr_val_dict).unfold().as_(vertex_type).\
                addV(vertex_type).as_('v').property(id,node_id).\
                sideEffect(__.select(vertex_type).unfold().as_('kv').select('v').\
                    property(__.select('kv').by(Column.keys),
                                __.select('kv').by(Column.values)
                                )
                    ).iterate()

            # Insert_edge

            to_node = g.V().has(id,node_id).next()
            if(not g.E().has(id,target_id+'-'+node_id).hasNext()):
                logger.info(f'Insert_Edge: {target_id} --> {node_id}.')
                g.V().has(id,target_id).addE('CATEGORY').to(to_node).property(id,target_id+'-'+node_id).iterate() 
        
        conn = self.gremlin_utils.remote_connection()
        g = self.gremlin_utils.traversal_source(connection=conn) 

        if (not g.V().has(id, target_id).hasNext()):
            logger.info(f'Insert_Vertex: {target_id}.')
            g.inject(tr_dict).unfold().as_(vertex_type).\
            addV(vertex_type).as_('v').property(id,target_id).\
            sideEffect(__.select(vertex_type).unfold().as_('kv').select('v').\
                property(__.select('kv').by(Column.keys),
                            __.select('kv').by(Column.values)
                            )
                ).iterate()         
                
        attr_cols = [f'val{x}' for x in range(1,391)]
        empty_node_dict ={}
        for attr in attr_cols:
            empty_node_dict[attr] = 0.0
            
        for node_k, node_v in connectted_node_dict[0].items():
            node_id = node_k + '-' + str(node_v)
            insert_attr(g, [empty_node_dict], target_id, node_id, vertex_type = node_k)   

        conn.close()                    
                    
    def query_target_subgraph(self, target_id, neighbor_cols, dummied_col):
        """Extract 2nd degree subgraph of target transaction.Dump data into subgraph dict and n_feats dict.
        subgraph_dict:  related transactions' id list and values through edges
        n_feats dict: related 1 degree vertex and transactions' embeded elements vectors. 
        Usually after insert new test sample's vertex and edges into graphDB. 
        
        Example:
        >>> query_target_subgraph('3661635', 'C1,C2,C3,C4,C5,C6,...', 'M2_T,M3_F,M3_T,...')
        """
        subgraph_dict = {}
        neighbor_list = []
        neighbor_dict = {}
        transaction_embed_value_dict = {}
        
        ii = 0
        s_t = dt.now()
        
        conn = self.gremlin_utils.remote_connection()
        g = self.gremlin_utils.traversal_source(connection=conn) 

        target_name = target_id[(target_id.find('-')+1):]
        feature_list = g.V().has(id,target_id).out().id().toList()
        for feat in feature_list:
            ii += 1
            feat_name = feat[:feat.find('-')]
            feat_value = feat[(feat.find('-')+1):]
            node_list = g.V().has(id,feat).both().limit(MAX_FEATURE_NODE).id().toList()
            target_and_conn_node_list = [int(target_name)]+[int(target_conn_node[(target_conn_node.find('-')+1):]) for target_conn_node in node_list]
            neighbor_list += target_and_conn_node_list
            nodes_and_feature_value_array = (target_and_conn_node_list,[feat_value]*len(target_and_conn_node_list))
            # nodes_and_feature_value_array = (np.array(target_and_conn_node_list),np.array([feat_value]*len(target_and_conn_node_list)))
            subgraph_dict['target<>'+feat_name] = nodes_and_feature_value_array
        
        e_t = dt.now()
        logger.info(f'INSIDE query_target_subgraph: subgraph_dict used {(e_t - s_t).total_seconds()} seconds')
        new_s_t = e_t
        
        neighbor_cols = neighbor_cols.split(',') + dummied_col.split(',')
        
        if MAX_FEATURE_NODE < 50:
            neighbor_list = set(neighbor_list)
            for node in neighbor_list:
                node_dict = g.V().has(id,'t-'+str(node)).valueMap().toList()[0]
                node_dict = [node_dict.get(key)[0] for key in neighbor_cols]
                neighbor_dict[str(node)] = node_dict
            
            e_t = dt.now()
            logger.info(f'INSIDE query_target_subgraph: neighbor_dict by 2 step for loop a list, used {(e_t - new_s_t).total_seconds()} seconds. Total test cost {(e_t - s_t).total_seconds()} seconds.')
            new_s_t = e_t
        else:
            neighbor_dict = {}
            target_node_dict = g.V().has(id,'t-'+str(target_name)).valueMap().toList()[0]
            neighbor_dict[str(target_name)] = [target_node_dict.get(key)[0] for key in neighbor_cols]

            node_dict = g.V().has(id,target_id).repeat(both().limit(MAX_FEATURE_NODE)).times(2).elementMap().toList()
            for item in node_dict:
                node = item.get(list(item)[0])
                node_value = node[(node.find('-')+1):]
                neighbor_dict[node_value] = [item.get(key) for key in neighbor_cols]
            logger.info(len(neighbor_dict))
            
            e_t = dt.now()
            logger.info(f'INSIDE query_target_subgraph: neighbor_dict by query once then ETL, used {(e_t - new_s_t).total_seconds()} seconds. Total test cost {(e_t - s_t).total_seconds()} seconds.')
            new_s_t = e_t
        
        attr_cols = ['val'+str(x) for x in range(1,391)]
        for attr in feature_list:
            attr_name = attr[:attr.find('-')]
            attr_value = attr[(attr.find('-')+1):]
            attr_dict = g.V().has(id,attr).valueMap().toList()[0]
            attr_dict = [attr_dict.get(key)[0] for key in attr_cols]
            attr_input_dict = {}
            attr_input_dict[attr_value] =  attr_dict
            transaction_embed_value_dict[attr_name] = attr_input_dict
        
        e_t = dt.now()
        logger.info(f'INSIDE query_target_subgraph: transaction_embed_value_dict used {(e_t - new_s_t).total_seconds()} seconds. Total test cost {(e_t - s_t).total_seconds()} seconds.')
        new_s_t = e_t
        
        transaction_embed_value_dict['target'] = neighbor_dict

        conn.close()   

        return subgraph_dict, transaction_embed_value_dict    


def invoke_endpoint_with_idx(endpointname, target_id, subgraph_dict, n_feats):
    """
    Post data input to and request response from sagemaker inference endpoint.
    
    Example:
    >>> invoke_endpoint_with_idx('frauddetection', '3636131', subgraph_dict, transaction_embed_value_dict)

    Args:
    
    endpointname: Neptune endpoint string from environ
    target_id: transaction id for inference. default to be event['TransactionID']
    subgraph_dict: testgraphpath
    n_feats: transaction_embed_values

    """
    
    payload = {
        'graph': subgraph_dict,
        'n_feats': n_feats,
        'target_id': target_id
    }

    response = runtime.invoke_endpoint(EndpointName=endpointname,
                                            ContentType='application/json',
                                            Body=json.dumps(payload))

    res_body = response['Body'].read()
    results = json.loads(res_body)

    pred_prob = results

    return pred_prob


def handler(event, context):

    logger.info('Endpoint name: {}'.format(ENDPOINT_NAME))
    
    G_s_t = dt.now()

    trans_dict, identity_dict, target_id = load_data_from_event(event, transactions_id_cols, identities_cols, neighbor_cols, dummied_col)
    
    G_e_t = dt.now()
    logger.info(f'load_data_from_event used {(G_e_t - G_s_t).total_seconds()} seconds. ')
    G_new_s_t = G_e_t
    
    graph_input = GraphModelClient(endpoints)
    graph_input.insert_new_transaction_vertex_and_edge(trans_dict, identity_dict , target_id, vertex_type = 'Transaction')
    
    G_e_t = dt.now()
    logger.info(f'insert_new_transaction_vertex_and_edge used {(G_e_t - G_new_s_t).total_seconds()} seconds. Total test cost {(G_e_t - G_s_t).total_seconds()} seconds.')
    G_new_s_t = G_e_t
    
    subgraph_dict, transaction_embed_value_dict = graph_input.query_target_subgraph(target_id, neighbor_cols, dummied_col)
    
    G_e_t = dt.now()
    logger.info(f'query_target_subgraph used {(G_e_t - G_new_s_t).total_seconds()} seconds. Total test cost {(G_e_t - G_s_t).total_seconds()} seconds.')
    G_new_s_t = G_e_t

    transaction_id = int(target_id[(target_id.find('-')+1):])
    
    pred_prob = invoke_endpoint_with_idx(endpointname = ENDPOINT_NAME, target_id = transaction_id, subgraph_dict = subgraph_dict, n_feats = transaction_embed_value_dict)
    
    e_t = dt.now()
    logger.info(f'invoke_endpoint_with_idx used {(G_e_t - G_new_s_t).total_seconds()} seconds. Total test cost {(G_e_t - G_s_t).total_seconds()} seconds.')
    G_new_s_t = G_e_t

    data_output = {
                    'timestamp': int(time.time()),
                    'isFraud': pred_prob > MODEL_BTW,
                    'id': event['TransactionID'],
                    'amount': event['TransactionAmt'],
                    'productCD': event['ProductCD'],
                    'card1': event['card1'],
                    'card2': event['card2'],
                    'card3': event['card3'],
                    'card4': event['card4'],
                    'card5': event['card5'],
                    'card6': event['card6'],
                    'addr1': event['addr1'],
                    'addr2': event['addr2'],
                    'dist1': event['dist1'],
                    'dist2': event['dist2'],
                    'pEmaildomain': event['P_emaildomain'],
                    'rEmaildomain': event['R_emaildomain'],
                }

    logger.info(f'Send transaction {data_output} to queue.')
    response = sqs.send_message(
        QueueUrl=QUEUE_URL,
        DelaySeconds=0,
        MessageBody=json.dumps(data_output),
        MessageGroupId=context.aws_request_id,
    )
    
    return f"Done. with flag:{pred_prob> MODEL_BTW} and pred_prob:  {pred_prob}"