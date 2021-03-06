import random

import tensorflow as tf
import tensorflow.layers
import numpy as np

import tflib as lib
import tflib.nn.conv2d
import tflib.nn.deconv2d
import tflib.nn.linear
from dqn.utils import LinearSchedule

def norm_state_Q_GAN(state):
    return tf.clip_by_value(state, -1*127.5/130., 127.5/130.)


class GDM():

    def __init__(self, session, config, num_actions=18):
        self.sess = session
        self.config = config
        self.num_actions = num_actions
        self.lookahead = self.config.lookahead
        self.history_length = self.config.history_length
        self.state_width = self.config.screen_width
        self.state_height = self.config.screen_height
        self.data_format = self.config.cnn_format
        self.gdm_ngf = self.config.gdm_ngf
        self.disc_ngf = self.config.disc_ngf
        self.gdm_weight_decay = self.config.gdm_weight_decay
        self.disc_weight_decay = self.config.disc_weight_decay
        self.lambda_l1 = self.config.lambda_l1
        self.lambda_l2 = self.config.lambda_l2

        self.initializer = tf.truncated_normal_initializer(0.0, 0.02)
        self.beta_initializer = tf.zeros_initializer()
        self.gamma_initializer = tf.truncated_normal_initializer(1.0, 0.02)
        # self.initializer = None
        self.gamma_initializer = None

        self.exploration_gan = LinearSchedule(50000, 0.01)
        self.gen_step = 0
        self.gan_warmup = config.gan_warmup

        self.actions = tf.placeholder(
            tf.int32, shape=[None, self.lookahead], name='actions')
        self.is_training = tf.placeholder(dtype=tf.bool, name='is_training')

        self.warmup = tf.placeholder(
            tf.bool, shape=[self.lookahead], name='gan_warmup')

        self.concat_dim = 1
        self.pre_state = tf.placeholder(
            tf.float32, shape=[None, self.history_length, self.state_width, self.state_height], name='pre_state')
        self.post_state = tf.placeholder(
            tf.float32, shape=[None, self.lookahead, self.state_width, self.state_height], name='post_state')

        if self.data_format == 'NHWC':
            self.concat_dim = 3
            self.pre_state = tf.transpose(
                self.pre_state, (0, 2, 3, 1), name='NCHW_to_NHWC')
            self.post_state = tf.transpose(
                self.post_state, (0, 2, 3, 1), name='NCHW_to_NHWC')

        # wgan-gp
        # self.lamda = self.config.lamda

        with tf.variable_scope('gdm'):
            self.predicted_state = norm_state_Q_GAN(self.build_gdm(
                self.pre_state, tf.expand_dims(self.actions[:, 0], axis=1), self.is_training, ngf=self.gdm_ngf))

        self.trajectories = self.pre_state
        with tf.variable_scope('gdm', reuse=True):
            for i in range(self.lookahead):
                self.state = norm_state_Q_GAN(self.build_gdm(
                    self.trajectories[:, -self.history_length:, ...], tf.expand_dims(self.actions[:, i], axis=1), self.is_training, ngf=self.gdm_ngf))
                self.trajectories = tf.concat(
                    [self.trajectories, self.state], axis=1)

        with tf.name_scope('opt'):
            # self.gdm_train_op, self.disc_train_op, self.gdm_summary, self.disc_summary, self.merged_summary = self.build_training_op()
            self.gdm_train_op, self.disc_train_op, self.gdm_summary, self.disc_summary = self.build_training_op()

    def get_state(self, states, actions):
        predicted_state = self.sess.run(self.trajectories, feed_dict={
            self.pre_state: states, self.actions: actions, self.is_training: False})
        return predicted_state

    def rollout(self, states, actions, num_rollout):
        for i in range(num_rollout):
            action = np.expand_dims(actions[i:i+self.lookahead], axis=0)
            predicted_state = self.sess.run(self.predicted_state, feed_dict={
                self.pre_state: states[:, -self.history_length:, ...], self.actions: action, self.is_training: False})
            states = np.concatenate([states, predicted_state], axis=1)
        return np.squeeze(states)

    def train(self, pre_state, action, post_state):
        warmup = []
        sample, gan_epsiron = 0, 0
        for _ in range(self.lookahead):
            if self.gen_step > self.gan_warmup:
                sample = random.random()
                gan_epsiron = self.exploration_gan.value(
                    self.gen_step-self.config.gan_warmup)
            else:
                sample = 1
                gan_epsiron = 0
            warmup.append(sample > gan_epsiron)
        _, _, disc_summary, gdm_summary = self.sess.run([self.disc_train_op, self.gdm_train_op, self.disc_summary, self.gdm_summary], feed_dict={
            self.pre_state: pre_state, self.post_state: post_state, self.actions: action, self.warmup: warmup, self.is_training: True})

        self.gen_step += 1

        return gdm_summary, disc_summary

    def disc_train(self, pre_state, action, post_state, iteration=1):
        for _ in range(iteration):
            _, disc_summary = self.sess.run([self.disc_train_op, self.disc_summary], feed_dict={
                self.pre_state: pre_state, self.post_state: post_state, self.actions: action, self.is_training: True})
        return disc_summary

    def gdm_train(self, pre_state, action, post_state, warmup, iteration=1):
        for _ in range(iteration):
            # _, gdm_summary, merged_summary = self.sess.run([self.gdm_train_op, self.gdm_summary, self.merged_summary], feed_dict={
            #     self.pre_state: pre_state, self.post_state: post_state, self.actions: action, self.warmup: warmup, self.is_training: True})
            _, gdm_summary = self.sess.run([self.gdm_train_op, self.gdm_summary], feed_dict={
                self.pre_state: pre_state, self.post_state: post_state, self.actions: action, self.warmup: warmup, self.is_training: True})
        return gdm_summary

    def build_gdm(self, state, action, is_training, lookahead=1, ngf=32):

        in_channels = self.history_length

        # encoder
        # (None, 84, 84, 4)

        with tf.variable_scope('Encoder'):

            encode1 = lib.nn.conv2d.Conv2D(
                'Conv1', in_channels, ngf, 4, state, initializer=self.initializer, weight_decay_scale=self.gdm_weight_decay, stride=2, pytorch_biases=True, padding_size=1, data_format=self.data_format)
            encode1 = tf.layers.batch_normalization(
                encode1, momentum=0.9, epsilon=1e-05, beta_initializer=self.beta_initializer, gamma_initializer=self.gamma_initializer, gamma_regularizer=tf.contrib.layers.l2_regularizer(scale=self.gdm_weight_decay), training=is_training, name='BN1')
            encode1 = tf.nn.leaky_relu(encode1, alpha=0.2, name='leaky_ralu1')
            # (None, 42, 42, 32)

            encode2 = lib.nn.conv2d.Conv2D(
                'Conv2', ngf, ngf*2, 4, encode1, initializer=self.initializer, weight_decay_scale=self.gdm_weight_decay, stride=2, pytorch_biases=True, padding='VALID', data_format=self.data_format)
            encode2 = tf.layers.batch_normalization(
                encode2, momentum=0.9, epsilon=1e-05, beta_initializer=self.beta_initializer, gamma_initializer=self.gamma_initializer, gamma_regularizer=tf.contrib.layers.l2_regularizer(scale=self.gdm_weight_decay), training=is_training, name='BN2')
            encode2 = tf.nn.leaky_relu(encode2, alpha=0.2, name='leaky_ralu2')
            # (None, 20, 20, 64)

            encode3 = lib.nn.conv2d.Conv2D(
                'Conv3', ngf*2, ngf*4, 4, encode2, initializer=self.initializer, weight_decay_scale=self.gdm_weight_decay, stride=2, pytorch_biases=True, padding_size=1, data_format=self.data_format)
            encode3 = tf.layers.batch_normalization(
                encode3, momentum=0.9, epsilon=1e-05, beta_initializer=self.beta_initializer, gamma_initializer=self.gamma_initializer, gamma_regularizer=tf.contrib.layers.l2_regularizer(scale=self.gdm_weight_decay), training=is_training, name='BN3')
            encode3 = tf.nn.leaky_relu(encode3, alpha=0.2, name='leaky_ralu3')
            # (None, 10, 10, 128)

            encode4 = lib.nn.conv2d.Conv2D(
                'Conv4', ngf*4, ngf*8, 4, encode3, initializer=self.initializer, weight_decay_scale=self.gdm_weight_decay, stride=2, pytorch_biases=True, padding_size=1, data_format=self.data_format)
            encode4 = tf.layers.batch_normalization(
                encode4, momentum=0.9, epsilon=1e-05, beta_initializer=self.beta_initializer, gamma_initializer=self.gamma_initializer, gamma_regularizer=tf.contrib.layers.l2_regularizer(scale=self.gdm_weight_decay), training=is_training, name='BN4')
            encode4 = tf.nn.leaky_relu(encode4, alpha=0.2, name='leaky_ralu4')
            # (None, 5, 5, 256)

            encode5 = lib.nn.conv2d.Conv2D(
                'Conv5', ngf*8, ngf*8, 3, encode4, initializer=self.initializer, weight_decay_scale=self.gdm_weight_decay, stride=1, pytorch_biases=True, padding_size=1, data_format=self.data_format)
            encode5 = tf.layers.batch_normalization(
                encode5, momentum=0.9, epsilon=1e-05, beta_initializer=self.beta_initializer, gamma_initializer=self.gamma_initializer, gamma_regularizer=tf.contrib.layers.l2_regularizer(scale=self.gdm_weight_decay), training=is_training, name='BN5')
            encode5 = tf.nn.leaky_relu(encode5, alpha=0.2, name='leaky_ralu5')
            # (None, 5, 5, 256)

            encode6 = lib.nn.conv2d.Conv2D(
                'Conv6', ngf*8, ngf*8, 3, encode5, initializer=self.initializer, weight_decay_scale=self.gdm_weight_decay, stride=1, pytorch_biases=True, padding_size=1, data_format=self.data_format)
            encode6 = tf.layers.batch_normalization(
                encode6, momentum=0.9, epsilon=1e-05, beta_initializer=self.beta_initializer, gamma_initializer=self.gamma_initializer, gamma_regularizer=tf.contrib.layers.l2_regularizer(scale=self.gdm_weight_decay), training=is_training, name='BN6')
            encode6 = tf.nn.leaky_relu(encode6, alpha=0.2, name='leaky_ralu6')
            # (None, 5, 5, 256)

        # Decoder

        with tf.variable_scope('Decoder'):

            action_one_hot = tf.one_hot(
                action, self.num_actions, name='action_one_hot')

            def create_action_tile(action_one_hot, shape, name='action_tile'):
                if self.data_format == 'NCHW':
                    action_one_hot = tf.reshape(
                        action_one_hot, [-1, self.num_actions*lookahead, 1, 1])
                    action_tile = tf.tile(
                        action_one_hot, [1, 1, shape[2], shape[3]], name=name)
                else:
                    action_one_hot = tf.reshape(
                        action_one_hot, [-1, 1, 1, self.num_actions*lookahead])
                    action_tile = tf.tile(
                        action_one_hot, [1, shape[1], shape[2], 1], name=name)
                return action_tile

            action_tile1 = create_action_tile(
                action_one_hot, encode6.shape, name='AT1')
            concat1 = tf.concat(
                [encode6, action_tile1], self.concat_dim, name='concat1')
            decode1 = lib.nn.deconv2d.Deconv2D(
                'Deconv1', ngf * 8 + self.num_actions*lookahead, ngf * 8, 3, concat1, initializer=self.initializer, weight_decay_scale=self.gdm_weight_decay, stride=1, biases=False, padding_size=1, data_format=self.data_format)
            decode1 = tf.layers.batch_normalization(
                decode1, momentum=0.9, epsilon=1e-05, beta_initializer=self.beta_initializer, gamma_initializer=self.gamma_initializer, gamma_regularizer=tf.contrib.layers.l2_regularizer(scale=self.gdm_weight_decay), training=is_training, name='BN1')
            decode1 = tf.nn.relu(decode1, name='relu1')
            # (None, 5, 5, 256)

            action_tile2 = create_action_tile(
                action_one_hot, decode1.shape, name='AT2')
            concat2 = tf.concat(
                [decode1, encode5, action_tile2], self.concat_dim, name='concat2')
            decode2 = lib.nn.deconv2d.Deconv2D(
                'Deconv2', ngf*8*2+self.num_actions*lookahead, ngf*8, 3, concat2, initializer=self.initializer, weight_decay_scale=self.gdm_weight_decay, stride=1, biases=False, padding_size=1, data_format=self.data_format)
            decode2 = tf.layers.batch_normalization(
                decode2, momentum=0.9, epsilon=1e-05, beta_initializer=self.beta_initializer, gamma_initializer=self.gamma_initializer, gamma_regularizer=tf.contrib.layers.l2_regularizer(scale=self.gdm_weight_decay), training=is_training, name='BN2')
            decode2 = tf.nn.relu(decode2, name='relu2')
            # (None, 5, 5, 256)

            action_tile3 = create_action_tile(
                action_one_hot, decode2.shape, name='AT3')
            concat3 = tf.concat(
                [decode2, encode4, action_tile3], self.concat_dim, name='concat3')
            decode3 = lib.nn.deconv2d.Deconv2D(
                'Deconv3', ngf*8*2+self.num_actions*lookahead, ngf*4, 4, concat3, initializer=self.initializer, weight_decay_scale=self.gdm_weight_decay, stride=2, biases=False, padding_size=1, data_format=self.data_format)
            decode3 = tf.layers.batch_normalization(
                decode3, momentum=0.9, epsilon=1e-05, beta_initializer=self.beta_initializer, gamma_initializer=self.gamma_initializer, gamma_regularizer=tf.contrib.layers.l2_regularizer(scale=self.gdm_weight_decay), training=is_training, name='BN3')
            decode3 = tf.nn.relu(decode3, name='relu3')
            # (None, 10, 10, 128)

            action_tile4 = create_action_tile(
                action_one_hot, decode3.shape, name='AT4')
            concat4 = tf.concat(
                [decode3, encode3, action_tile4], self.concat_dim, name='concat4')
            decode4 = lib.nn.deconv2d.Deconv2D(
                'Deconv4', ngf*4*2+self.num_actions*lookahead, ngf*2, 4, concat4, initializer=self.initializer, weight_decay_scale=self.gdm_weight_decay, stride=2, biases=False, padding_size=1, data_format=self.data_format)
            decode4 = tf.layers.batch_normalization(
                decode4, momentum=0.9, epsilon=1e-05, beta_initializer=self.beta_initializer, gamma_initializer=self.gamma_initializer, gamma_regularizer=tf.contrib.layers.l2_regularizer(scale=self.gdm_weight_decay), training=is_training, name='BN4')
            decode4 = tf.nn.relu(decode4, name='relu4')
            # (None, 20, 20, 64)

            action_tile5 = create_action_tile(
                action_one_hot, decode4.shape, name='AT5')
            concat5 = tf.concat(
                [decode4, encode2, action_tile5], self.concat_dim, name='concat5')
            decode5 = lib.nn.deconv2d.Deconv2D(
                'Deconv5', ngf*2*2+self.num_actions*lookahead, ngf, 4, concat5, initializer=self.initializer, weight_decay_scale=self.gdm_weight_decay, stride=2, biases=False, padding='VALID', data_format=self.data_format)
            decode5 = tf.layers.batch_normalization(
                decode5, momentum=0.9, epsilon=1e-05, beta_initializer=self.beta_initializer, gamma_initializer=self.gamma_initializer, gamma_regularizer=tf.contrib.layers.l2_regularizer(scale=self.gdm_weight_decay), training=is_training, name='BN5')
            decode5 = tf.nn.relu(decode5, name='relu5')
            # (None, 42, 42, 32)

            action_tile6 = create_action_tile(
                action_one_hot, decode5.shape, name='AT6')
            concat6 = tf.concat(
                [decode5, action_tile6], self.concat_dim, name='concat6')
            decode6 = lib.nn.deconv2d.Deconv2D(
                'Deconv6', ngf+self.num_actions*lookahead, lookahead, 4, concat6, initializer=self.initializer, weight_decay_scale=self.gdm_weight_decay, stride=2, biases=False, padding_size=1, data_format=self.data_format)
            decode6 = tf.nn.tanh(decode6, name='tanh')
            # (None, 84, 84, lookahead)

        return decode6

    def build_discriminator(self, state, action, is_training=False, update_collection=None, ngf=64):

        output = lib.nn.conv2d.Conv2D(
            'Conv1', self.history_length + self.lookahead, ngf, 8, state, initializer=self.initializer, weight_decay_scale=self.disc_weight_decay, spectral_norm=True, update_collection=update_collection, stride=4, pytorch_biases=True, padding_size=1, data_format=self.data_format)
        output = tf.layers.batch_normalization(
            output, momentum=0.9, epsilon=1e-05, beta_initializer=self.beta_initializer, gamma_initializer=self.gamma_initializer, gamma_regularizer=tf.contrib.layers.l2_regularizer(scale=self.disc_weight_decay), training=is_training, name='BN1')
        output = tf.nn.leaky_relu(output, 0.2)
        # (None, 20, 20, 64)

        output = lib.nn.conv2d.Conv2D(
            'Conv2', ngf, ngf * 2, 4, output, initializer=self.initializer, weight_decay_scale=self.disc_weight_decay, spectral_norm=True, update_collection=update_collection, stride=2, pytorch_biases=True, padding_size=1, data_format=self.data_format)
        output = tf.layers.batch_normalization(
            output, momentum=0.9, epsilon=1e-05, beta_initializer=self.beta_initializer, gamma_initializer=self.gamma_initializer, gamma_regularizer=tf.contrib.layers.l2_regularizer(scale=self.disc_weight_decay), training=is_training, name='BN2')
        output = tf.nn.leaky_relu(output, 0.2)
        # (None, 10, 10, 128)

        output = lib.nn.conv2d.Conv2D(
            'Conv3', ngf * 2, ngf * 4, 4, output, initializer=self.initializer, weight_decay_scale=self.disc_weight_decay, spectral_norm=True, update_collection=update_collection, stride=2, pytorch_biases=True, padding_size=1, data_format=self.data_format)
        output = tf.layers.batch_normalization(
            output, momentum=0.9, epsilon=1e-05, beta_initializer=self.beta_initializer, gamma_initializer=self.gamma_initializer, gamma_regularizer=tf.contrib.layers.l2_regularizer(scale=self.disc_weight_decay), training=is_training, name='BN3')
        output = tf.nn.leaky_relu(output, 0.2)
        # (None, 7, 7, 256)

        output = lib.nn.conv2d.Conv2D(
            'Conv.4', ngf * 4, 16, 3, output, initializer=self.initializer, weight_decay_scale=self.disc_weight_decay, spectral_norm=True, update_collection=update_collection, stride=1, pytorch_biases=True, padding_size=1, data_format=self.data_format)
        output = tf.layers.batch_normalization(
            output, momentum=0.9, epsilon=1e-05, beta_initializer=self.beta_initializer, gamma_initializer=self.gamma_initializer, gamma_regularizer=tf.contrib.layers.l2_regularizer(scale=self.disc_weight_decay), training=is_training, name='BN4')
        # (None, 5, 5, 16)

        output = tf.layers.flatten(output, name='flatten')

        # (None, 400)

        action_one_hot = tf.one_hot(
            action, self.num_actions, name='action_one_hot')

        action_one_hot = tf.layers.flatten(
            action_one_hot, name='action_one_hot_flatten')

        output = tf.concat([output, action_one_hot],
                           self.concat_dim, name='concat1')

        output = lib.nn.linear.Linear(
            'Dence1', 16 * 25 + self.num_actions*self.lookahead, 18, output, initialization='pytorch', weight_decay_scale=self.disc_weight_decay, spectral_norm=False, pytorch_biases=True, update_collection=update_collection)
        output = tf.nn.leaky_relu(output, 0.2)
        # (None, 18)

        output = tf.concat([output, action_one_hot],
                           self.concat_dim, name='concat2')
        # (None, 18+num_actions*lookahead)

        output = lib.nn.linear.Linear(
            'Dence2', 18 + self.num_actions*self.lookahead, 1, output, initialization='pytorch', weight_decay_scale=self.disc_weight_decay, spectral_norm=False, pytorch_biases=True, update_collection=update_collection)
        # (None, 3*lookahead)

        output = tf.reshape(output, [-1])

        return output

    def build_training_op(self):

        real_state = tf.concat(
            [self.pre_state, self.post_state], axis=self.concat_dim, name='real_state')

        fake_state = self.pre_state

        with tf.variable_scope('gdm', reuse=True):
            for i in range(self.lookahead):
                fake_state = tf.cond(
                    self.warmup[i],
                    lambda: tf.concat(
                        [fake_state, norm_state_Q_GAN(
                            self.build_gdm(
                                    fake_state[:, -self.history_length:, ...],
                                    tf.expand_dims(self.actions[:, i], axis=1),
                                    self.is_training,
                                    ngf=self.gdm_ngf))
                                ],
                                axis=1),
                    lambda: tf.concat(
                        [fake_state, norm_state_Q_GAN(
                            self.build_gdm(
                                tf.concat([fake_state[:, -self.history_length:-1, ...], real_state[:, self.history_length+i-1:self.history_length+i, ...]], axis=1),
                                tf.expand_dims(self.actions[:, i],axis=1),
                                self.is_training,
                                ngf=self.gdm_ngf))
                                ],
                                axis=1)
                            )

        with tf.name_scope('disc_fake'):
            with tf.variable_scope('discriminator'):
                disc_fake = self.build_discriminator(
                    fake_state, self.actions, self.is_training, update_collection=None, ngf=self.disc_ngf)

        with tf.name_scope('disc_real'):
            with tf.variable_scope('discriminator', reuse=True):
                disc_real = self.build_discriminator(
                    real_state, self.actions, self.is_training, update_collection='NO_OPS', ngf=self.disc_ngf)

        with tf.name_scope('loss'):
            gdm_loss = -tf.reduce_mean(disc_fake)
            disc_loss = tf.reduce_mean(disc_fake) - tf.reduce_mean(disc_real)

            # Gradient penalty
            # with tf.name_scope('gradient_penalty'):
            #     alpha = tf.random_uniform(
            #         shape=[tf.shape(real_state)[0], 1, 1, 1],
            #         minval=0.,
            #         maxval=1.,
            #         name='alpha'
            #     )
            #     differences = fake_state - real_state
            #     interpolates = real_state + (alpha * differences)

            #     with tf.variable_scope('discriminator', reuse=True):
            #         delta = self.build_discriminator(
            #             interpolates, action, is_training, update_collection='NO_OPS')

            #     gradients = tf.gradients(delta, [interpolates])[0]
            #     slopes = tf.sqrt(tf.reduce_sum(
            #         tf.square(gradients), reduction_indices=[1]))
            #     gradient_penalty = tf.reduce_mean(
            #         (slopes-1.)**2, name='gradient_penalty')
            #     disc_loss += self.lamda * gradient_penalty

        with tf.name_scope('weight_decay'):
            gdm_weight_decay = tf.losses.get_regularization_loss(
                scope='gdm', name='gdm_weight_decay')
            disc_weight_decay = tf.losses.get_regularization_loss(
                scope='discriminator', name='disc_weight_decay')

            gdm_loss += gdm_weight_decay
            disc_loss += disc_weight_decay

        gdm_summary = tf.summary.scalar('gdm_loss', gdm_loss)
        disc_summary = tf.summary.scalar('disc_loss', disc_loss)

        with tf.name_scope('L1_L2_loss'):
            difference = fake_state[:, -self.lookahead:, ...] - self.post_state
            l1_loss = tf.reduce_mean(tf.abs(difference))
            # l1_summary = tf.summary.scalar('l1_loss', l1_loss)
            l2_loss = tf.reduce_mean(tf.square(difference))
            # l2_summary = tf.summary.scalar('l2_loss', l2_loss)
            gdm_loss = l2_loss * self.lambda_l2 + l1_loss * self.lambda_l1 + gdm_loss
            # gan_summary = tf.summary.scalar('gdm_L1_L2_loss', gdm_loss)
            # merged_summary = tf.summary.merge(
            #     [gan_summary, l1_summary, l2_summary], name='merged_summary')

        gdm_params = tf.get_collection(
            tf.GraphKeys.TRAINABLE_VARIABLES, scope='gdm')
        disc_params = tf.get_collection(
            tf.GraphKeys.TRAINABLE_VARIABLES, scope='discriminator')

        # For batch normalization
        with tf.control_dependencies(tf.get_collection(tf.GraphKeys.UPDATE_OPS, scope='gdm')):
            gdm_train_op = tf.train.AdamOptimizer(
                learning_rate=1e-4, beta1=0.5, beta2=0.999, name='gdm_adam').minimize(gdm_loss, var_list=gdm_params)

        with tf.control_dependencies(tf.get_collection(tf.GraphKeys.UPDATE_OPS, scope='opt')):
            disc_train_op = tf.train.MomentumOptimizer(
                learning_rate=1e-5, momentum=0.9, name='disc_SGD').minimize(disc_loss, var_list=disc_params)

        return gdm_train_op, disc_train_op, gdm_summary, disc_summary
