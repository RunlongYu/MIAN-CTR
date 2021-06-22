# -*- coding: utf-8 -*-

"""
 * @file    graph_layers.py
 * @author  chenye@antfin.com
 * @date    2019/8/11 9:47 PM
 * @brief   
"""

import tensorflow as tf
import logging
import keras

from alps.common.model import BaseModel
from alps.common.layers.keras.layer_parser import LayerParser
from keras.layers import *

from keras.engine import Model
from alps.common.layers.keras.ast_parser import add
from alps.common.utils.model_util import get_embedding_for_sparse
from alps.common.config.pyhocon import ConfigTree

from utils import build_addon_branch, dice, dense2sparse


class ModelWnd(BaseModel):
    def __init__(self, config):
        super(ModelWnd, self).__init__(config)
        pass

    def get_column(self, name):
        for item in self.config.x:
            if item.feature_name == name:
                return item
        return None

    def build_model(self, inputs, labels):

        embedding_dim = self.config.x[0].embedding_dim
        for m in self.config.x:
            if 'dense' in m['type']:
                self.config.use_dense = True

        logging.info("embedding_dim: %s" % embedding_dim)
        logging.info("use_dense: %s" % self.config.use_dense)

        parser = LayerParser(self.config, inputs)
        # deep_input_emb的shape(group_num, sample_num, embedding_dim)
        deep_input, deep_input_emb = parser.get_layer0(("deep_features_sparse", embedding_dim))
        # wide_input_emb的shape（1，sample_num, 1）
        wide_input, wide_input_emb = parser.get_layer0(("wide_features", 1))

        input_list = []
        add(input_list, deep_input)
        add(input_list, wide_input)

        dnn_emb_ctr = Concatenate(axis=-1)(deep_input_emb)

        # seq feature shape (sample_num, max_len)
        if self.get_column("seq_features"):
            feature_dim = self.config.attention.get('feature_dim', None)
            embed_dim = self.config.attention.get('embedding_dim', 8)
            seq_feature_len = self.config.attention.get('seq_feature_len', None)
            if seq_feature_len is None or feature_dim is None:
                raise Exception('seq_feature_len or feature_dim is missed in the configuration')
            logging.info("seq_feature_len: %s" % seq_feature_len)
            logging.info("seq embed_dim: %s" % embed_dim)

            seq_features = inputs["seq_features"]
            seq_input_list = dense2sparse(seq_features, feature_dim, seq_feature_len, convert2keras=True)
            seq_embed_list = []
            layer_buffer = []
            item_config = ConfigTree({
                'feature_name': 'seq_fea',
                'type': 'sparse',
                'shape': [feature_dim],
                'embedding_dim': embedding_dim,
                'group': 0
            })
            seq_input_0, seq_input_emb_0 = get_embedding_for_sparse(self.config, item_config, [seq_input_list[0]],
                                                                    use_weight=False, layer_buffer=layer_buffer)
            # TODO: support share_embedding=False
            add(input_list, seq_input_0)
            add(seq_embed_list, seq_input_emb_0)
            sparse_layer = layer_buffer[-1]
            for group, seq_input_item in enumerate(seq_input_list):
                if group == 0:
                    continue
                seq_input_emb = sparse_layer(seq_input_item)
                add(input_list, seq_input_item)
                add(seq_embed_list, seq_input_emb)

            attention_input_list = self.config.attention.attention_input
            attention_stop_gradient = self.config.attention.stop_gradient_on_input
            attention_embed_list = []
            for i in attention_input_list:
                attention_embed_list.append(deep_input_emb[i])
            attention_embedding_input = Concatenate(axis=-1)(attention_embed_list)

            params = dict(config=self.config.attention)

            if attention_stop_gradient:
                input_tensor_list = seq_embed_list + [Lambda(lambda x: K.stop_gradient(x))(attention_embedding_input)]
            else:
                input_tensor_list = seq_embed_list + [attention_embedding_input]

            attention_embed = Lambda(self._attention_layer, arguments=params,
                                     output_shape=lambda shapes: shapes[0])(input_tensor_list)

            dnn_emb_ctr = Concatenate(axis=-1)([dnn_emb_ctr, attention_embed])

        addon_branch = self.config.model_def.get('addon_branch', None)
        if addon_branch:
            addon_branch_args = self.config.model_def.get('addon_branch_args', {})
            logging.info("addon branch: %s" % addon_branch)
            addon_embed = build_addon_branch(addon_branch, deep_input_emb, addon_branch_args)
            dnn_emb_ctr = Concatenate(axis=-1)([dnn_emb_ctr, addon_embed])

        # deep dense，如果有才运行
        if self.config.use_dense:
            dense_input, dense_input_tmp = parser.get_layer0("deep_features_dense")
            add(input_list, dense_input)

            # 对dense值处理
            if self.config.model_def.dense_batch_norm:
                momentum = self.config.model_def.get('batch_norm_momentum', 0.99)
                dense_input = BatchNormalization(momentum=momentum)(dense_input)
                dense_input = Dense(dense_input.shape[1].value, init='he_normal',
                                    W_regularizer=regularizers.L1L2(0.0, 1e-3),
                                    b_regularizer=regularizers.L1L2(0.0, 1e-3),
                                    name='fc_deep_input')(dense_input)

            dnn_emb_ctr = Concatenate(axis=-1)([dnn_emb_ctr, dense_input])

        x = dnn_emb_ctr

        act = self.config.model_def.get('activation', 'relu')

        layer_dims = self.config.model_def.get('deep_layers_dim', [16, 8, 4, 1])
        for i, layer_dim in enumerate(layer_dims):
            if act == 'prelu':
                x = advanced_activations.PReLU(name='prelu_{}'.format(i))(x)
            elif act == 'dice':
                x = Lambda(lambda t: dice(t, name='dice_{}'.format(i)), name='lambda_dice_{}'.format(i))(x)
            else:
                x = Activation(act)(x)

            dense = Dense(layer_dim, init='he_normal',
                          W_regularizer=regularizers.L1L2(0.0, 1e-3),
                          b_regularizer=regularizers.L1L2(0.0, 1e-3),
                          name='fc{}_{}'.format(i, layer_dim))
            x = dense(x)

        branch_list = [x, wide_input_emb[0]]

        #logits = merge(branch_list, mode='sum')
        logits = keras.layers.add(branch_list)

        model = Model(input_list, logits, name="ModelWnd")
        model.summary()

        label = tf.cast(labels['label'], tf.float32)

        loss = tf.nn.sigmoid_cross_entropy_with_logits(labels=label,
                                                       logits=logits,
                                                       name='loss')

        self.loss = tf.reduce_mean(loss)
        self.predict_result = tf.sigmoid(logits)

        auc, auc_op = tf.metrics.auc(
            labels=label,
            predictions=self.predict_result, num_thresholds=10240)

        ctr = tf.reduce_sum(label) / tf.cast(tf.shape(label)[0], tf.float32)
        pcopc = tf.reduce_sum(self.predict_result) / tf.reduce_sum(label)
        pos_pred_avg = tf.div(tf.reduce_sum(self.predict_result * label), tf.maximum(tf.reduce_sum(label), 1))
        neg_pred_avg = tf.div(tf.reduce_sum(self.predict_result * (1 - label)), tf.maximum(tf.reduce_sum(1 - label), 1))

        # 计算准确率v
        accuracy = tf.reduce_mean(tf.cast(tf.equal(label, tf.round(self.predict_result)), dtype=tf.float32))
        self.metrics = {'ctr_accuracy': accuracy,
                        'auc': auc_op,
                        'loss': self.loss,
                        'ctr': ctr,
                        'pcopc': pcopc}

        tf.summary.scalar("00.train_auc", auc)
        tf.summary.scalar("01.train_loss", self.loss)
        tf.summary.scalar("02.ctr", ctr)
        tf.summary.scalar("03.pcopc", pcopc)
        tf.summary.scalar("04.pos_pred_avg", pos_pred_avg)
        tf.summary.scalar("05.neg_pred_avg", neg_pred_avg)

        return self.predict_result

    def _attention_layer(self, inputs, config):
        seq_item_num = config.get('seq_feature_len', None)
        nb_heads = config.get('multihead', 1)
        embedding_dim = config.get('embedding_dim', 8)
        dropout = config.get('dropout', 0)

        assert len(inputs) == seq_item_num + 1, "len(inputs) is not equal to seq_item_num + 1"
        seq_embed_list = inputs[:seq_item_num]
        attention_embed = inputs[-1]

        assert nb_heads >= 1, 'multi-head must be larger than 1'
        if nb_heads == 1:
            atten_wts = self._attention_weights(seq_embed_list, attention_embed, config)

            atten = tf.reduce_sum(
                tf.multiply(
                    tf.stack(seq_embed_list, axis=1),
                    tf.expand_dims(atten_wts, axis=-1)),
                axis=1)

        else:
            atten_list = []

            for head in range(nb_heads):
                with tf.name_scope("head_{}".format(head)):
                    atten_wts = self._attention_weights(seq_embed_list, attention_embed, config)

                    item_sum = tf.reduce_sum(
                        tf.multiply(
                            tf.stack(seq_embed_list, axis=1),
                            tf.expand_dims(atten_wts, axis=-1)),
                        axis=1)

                    atten_list.append(item_sum)

            atten = tf.concat(atten_list, axis=-1, name='atten_items_concat')

            atten = Dense(embedding_dim, init='he_normal',
                          W_regularizer=regularizers.L1L2(0.0, 1e-3),
                          name='fc_atten_{}'.format(embedding_dim),
                          use_bias=False)(atten)

        atten = Dropout(dropout)(atten)
        return atten

    def _attention_weights(self, item_list, user_embed, config):
        activations = config.get('layer_activations', ['relu', 'linear'])
        layer_dims = config.get('layer_dims', [48, 1])
        if layer_dims[-1] != 1:
            layer_dims.append(1)
        assert len(layer_dims) == len(activations), 'lengths of layer_dims and layer_activations must be equal'

        l1 = config.get('l1', 0)
        l2 = config.get('l2', 0)

        with tf.name_scope("attention_weights", [item_list, user_embed]):
            attention_weights = []

            for index, item in enumerate(item_list):
                x = tf.concat(
                    values=[item, user_embed],
                    axis=1,
                    name='item_user_embed_%s' % index)

                for layer_dim, act in zip(layer_dims, activations):
                    x = Dense(layer_dim, init='he_normal',
                              W_regularizer=regularizers.L1L2(l1, l2),
                              b_regularizer=regularizers.L1L2(l1, l2),
                              name='pp_list/pfc_i{}_l{}'.format(index, layer_dim),
                              activation=act
                              )(x)

                attention_weights.append(x)

            concat_attention = tf.concat(
                attention_weights,
                axis=1,
                name='attention_concat')

            attention_softmax = tf.nn.softmax(
                concat_attention,
                axis=-1,
                name='attention_softmax')

            return attention_softmax

    def get_prediction_result(self, **options):
        return None

    def get_loss(self, **options):
        return self.loss

    def get_metrics(self, **kwargs):
        return self.metrics

    def get_summary_op(self):
        return tf.summary.merge_all(), None

    @property
    def name(self):
        return 'ModelWnd'
