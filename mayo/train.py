import time
import math

import numpy as np
import tensorflow as tf

from mayo.net import Net
from mayo.util import memoize, import_from_dot_path
from mayo.preprocess import Preprocess
from mayo.checkpoint import CheckpointHandler


def _average_gradients(tower_grads):
    average_grads = []
    for grad_and_vars in zip(*tower_grads):
        grads = []
        for g, _ in grad_and_vars:
            # add 0 dimension to the gradients to represent the tower
            g = tf.expand_dims(g, 0)
            grads.append(g)
        # average over the 'tower' dimension.
        grad = tf.concat(axis=0, values=grads)
        grad = tf.reduce_mean(grad, 0)
        # simply return the first tower's pointer to the Variable
        v = grad_and_vars[0][1]
        grad_and_var = (grad, v)
        average_grads.append(grad_and_var)
    return average_grads


class Train(object):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self._graph = tf.Graph()
        self._preprocessor = Preprocess(self.config)

    @property
    @memoize
    def global_step(self):
        initializer = tf.constant_initializer(0)
        global_step = tf.get_variable(
            'global_step', [], initializer=initializer, trainable=False)
        return global_step

    @property
    @memoize
    def learning_rate(self):
        params = self.config.train.learning_rate
        steps_per_epoch = self.config.dataset.num_examples_per_epoch.train
        # 1 step == 1 batch
        steps_per_epoch /= self.config.train.batch_size
        decay_steps = int(steps_per_epoch * params.num_epochs_per_decay)
        return tf.train.exponential_decay(
            params.initial, self.global_step, decay_steps,
            params.decay_factor, staircase=True)

    @property
    @memoize
    def optimizer(self):
        params = self.config.train.optimizer
        optimizer_class = import_from_dot_path(params.pop('type'))
        return optimizer_class(self.learning_rate, **params)

    def tower_loss(self, images, labels, reuse):
        self._net = Net(
            self.config, images, labels, True, graph=self._graph, reuse=reuse)
        return self._net.loss()

    def _setup_gradients(self):
        config = self.config.train
        # ensure batch size is divisible by number of gpus
        if config.batch_size % config.num_gpus != 0:
            raise ValueError(
                'Batch size must be divisible by number of devices')
        # initialize images and labels
        images_splits, labels_splits = self._preprocessor.preprocess_train()
        # for each gpu
        iterator = enumerate(zip(images_splits, labels_splits))
        tower_grads = []
        reuse = None
        for i, (images_split, label_split) in iterator:
            # loss with the proper nested contexts
            name = 'tower_{}'.format(i)
            with tf.device('/gpu:{}'.format(i)), tf.name_scope(name):
                # loss from the final tower
                self._loss = self.tower_loss(
                    images_split, label_split, reuse)
                reuse = True
                # batch norm updates from the final tower
                with self._graph.as_default():
                    # summaries from the final tower
                    summaries = tf.get_collection(tf.GraphKeys.SUMMARIES)
                    self._batch_norm_updates = tf.get_collection(
                        tf.GraphKeys.UPDATE_OPS, name)
                # gradients from all towers
                grads = self.optimizer.compute_gradients(self._loss)
                tower_grads.append(grads)
        self._gradients = _average_gradients(tower_grads)
        # summaries
        summaries += [
            tf.summary.scalar('learning_rate', self.learning_rate),
            tf.summary.scalar('loss', self._loss)]
        self._summary_op = tf.summary.merge(summaries)

    def _setup_train_operation(self):
        app_grad_op = self.optimizer.apply_gradients(
            self._gradients, global_step=self.global_step)
        var_avgs = tf.train.ExponentialMovingAverage(
            self.config.train.moving_average_decay, self.global_step)
        var_avgs_op = var_avgs.apply(
            tf.trainable_variables() + tf.moving_average_variables())
        bn_op = tf.group(*self._batch_norm_updates)
        self._train_op = tf.group(app_grad_op, var_avgs_op, bn_op)

    def _init_session(self):
        # build an initialization operation to run below
        init = tf.global_variables_initializer()
        config = tf.ConfigProto(allow_soft_placement=True)
        self._session = tf.Session(config=config)
        self._session.run(init)

    def _to_epoch(self, step):
        epoch = step * self.config.train.batch_size
        return epoch / float(self.config.dataset.num_examples_per_epoch.train)

    def _update_progress(self, step, losses):
        now = time.time()
        duration = now - getattr(self, '_prev_time', now)
        epoch = self._to_epoch(step)
        loss = sum(losses) / len(losses)
        info = 'epoch {:.5}, average loss = {:.4} '.format(epoch, loss)
        if duration != 0:
            num_steps = step - getattr(self, '_prev_step', step)
            imgs_per_sec = num_steps * self.config.train.batch_size
            imgs_per_sec /= float(duration)
            info += '({:.1f} imgs/sec)'.format(imgs_per_sec)
        print(info)
        self._prev_time = now
        self._prev_step = step

    @property
    @memoize
    def _summary_writer(self):
        return tf.summary.FileWriter('summaries/', graph=self._graph)

    def _save_summary(self, step):
        print('Saving summaries...')
        summary = self._session.run(self._summary_op)
        self._summary_writer.add_summary(summary, step)

    def _train(self):
        print('Instantiating...')
        self._setup_gradients()
        self._setup_train_operation()
        self._init_session()
        checkpoint = CheckpointHandler(
            self._session, self.config.name, self.config.dataset.name)
        step = checkpoint.load()
        curr_step = 0
        tf.train.start_queue_runners(sess=self._session)
        self._net.save_graph()
        print('Training start')
        # train iterations
        config = self.config.train
        losses = []
        prev_epoch = math.floor(self._to_epoch(step))
        try:
            while step < config.max_steps:
                _, loss = self._session.run([self._train_op, self._loss])
                if np.isnan(loss):
                    raise ValueError('model diverged with a nan-valued loss')
                losses.append(loss)
                is_starting = curr_step < 100 and curr_step % 10 == 0
                if curr_step % 100 == 0 or is_starting:
                    self._update_progress(step, losses)
                    losses = []
                if curr_step % 1000 == 0:
                    self._save_summary(step)
                curr_step += 1
                if curr_step % 5000 == 0 or curr_step == config.max_steps:
                    checkpoint.save(step)
                epoch = math.floor(self._to_epoch(step))
                if epoch > prev_epoch:
                    prev_epoch = epoch
                    self.eval(self._net, checkpoint)
                step += 1
        except KeyboardInterrupt:
            print('Stopped, saving checkpoint in 3 seconds.')
        try:
            time.sleep(3)
        except KeyboardInterrupt:
            return
        checkpoint.save(step)

    def train(self):
        with self._graph.as_default(), tf.device('/cpu:0'):
            self._train()

    def eval(self, net, checkpoint):
        ...
