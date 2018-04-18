import math

import tensorflow as tf

from mayo.log import log
from mayo.util import Percent, memoize_property, object_from_params
from mayo.session.base import Session


class Train(Session):
    mode = 'train'

    def __init__(self, task, config):
        super().__init__(config)
        self._run_train_ops = True
        self._setup_train_operation()
        self._setup_summaries()
        self._init()
        self._checkpoint_epoch = ''

    @memoize_property
    def learning_rate(self):
        params = self.config.train.learning_rate
        lr_class, params = object_from_params(params)
        if lr_class is tf.train.piecewise_constant:
            # `tf.train.piecewise_constant` uses argument name 'x' instead
            # just to make life more difficult
            step_name = 'x'
        else:
            step_name = 'global_step'
        params[step_name] = self.num_epochs
        log.debug(
            'Using learning rate {!r} with params {}.'
            .format(lr_class.__name__, params))
        return lr_class(**params)

    @memoize_property
    def loss(self):
        return tf.reduce_mean(self.task.map(self.task.train))

    @memoize_property
    def optimizer(self):
        params = self.config.train.optimizer
        optimizer_class, params = object_from_params(params)
        log.debug('Using optimizer {!r}.'.format(optimizer_class.__name__))
        return optimizer_class(self.learning_rate, **params)

    @staticmethod
    def _average_gradients(tower_grads):
        tower_grads = list(tower_grads)
        if len(tower_grads) == 1:
            return tower_grads[0]
        average_grads = []
        for grad_and_vars in zip(*tower_grads):
            grads = []
            for g, v in grad_and_vars:
                # add 0 dimension to the gradients to represent the tower
                if g is None:
                    raise ValueError(
                        'Gradient for variable {} is None, please check '
                        'connection.'.format(v))
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

    @staticmethod
    def _loss_formatter(key, name):
        def formatter(estimator):
            loss_mean, loss_std = estimator.get_mean_std(key)
            if math.isnan(loss_mean):
                raise ValueError('Model diverged with a nan-valued loss.')
            loss_std = '±{}'.format(Percent(loss_std / loss_mean))
            return '{}: {:10f}{:5}'.format(name, loss_mean, loss_std)
        return formatter

    @memoize_property
    def _gradients(self):
        formatter = self._loss_formatter('regularization', 'regu')
        regularization = tf.get_collection(
            tf.GraphKeys.REGULARIZATION_LOSSES)
        if regularization:
            self.estimator.register(
                tf.add_n(regularization), 'regularization',
                formatter=formatter)

        def gradient(net, prediction, truth):
            loss = [self.task.train(prediction, truth)] + regularization
            return self.optimizer.compute_gradients(tf.add_n(loss))

        tower_grads = self.task.map(gradient)
        return self._average_gradients(tower_grads)

    def _setup_train_operation(self):
        ops = {}
        ops['app_grad'] = self.optimizer.apply_gradients(self._gradients)
        # update ops
        update_ops = list(self.get_collection(tf.GraphKeys.UPDATE_OPS))
        ops['update'] = tf.group(*update_ops, name='update')
        log.debug('Using update operations: {}'.format(update_ops))
        log.debug('Using training operations: {}'.format(ops))
        if self.extra_train_ops:
            ops['extra'] = self.extra_train_ops
        self._train_op = ops

    def _setup_summaries(self):
        if not self.config.system.summary.save:
            return
        summaries = list(self.get_collection(tf.GraphKeys.SUMMARIES))
        summaries += [
            tf.summary.scalar('learning_rate', self.learning_rate),
            tf.summary.scalar('loss', self.loss)]
        self._summary_op = tf.summary.merge(summaries)

    def _init(self):
        self.load_checkpoint(self.config.system.checkpoint.load)
        formatter = self._loss_formatter('loss', 'loss')
        self.estimator.register(self.loss, 'loss', formatter=formatter)

    @memoize_property
    def _summary_writer(self):
        path = self.config.system.search_path.summary[0]
        return tf.summary.FileWriter(path, graph=self.tf_graph)

    def _save_summary(self, epoch):
        summary = self.run(self._summary_op)
        self._summary_writer.add_summary(summary, epoch)

    def reset_num_epochs(self):
        log.info('Reseting number of training epochs of the model...')
        self.run(self.imgs_seen.initializer)
        self.change.reset('checkpoint.epoch')
        self.change.reset('step')

    def once(self):
        train_op = self._train_op if self._run_train_ops else []
        tasks = [train_op, self.num_epochs]
        noop, num_epochs = self.run(tasks, batch=True)
        return num_epochs

    def _overriders_call(self, func_name):
        # it is sufficient to use the first net, as overriders
        # share internal variables
        for o in self.nets[0].overriders:
            getattr(o, func_name)()

    def overriders_assign(self):
        log.info('Assigning overridden values of parameters to parameters...')
        self._overriders_call('assign')

    def overriders_update(self):
        log.info('Updating overrider internal variables...')
        self._overriders_call('update')

    def overriders_reset(self):
        log.info('Resetting overriders internal variables...')
        self._overriders_call('reset')

    def _iteration(self):
        system = self.config.system
        epoch = self.once()
        summary_interval = system.summary.get('save.interval', 0)
        if self.change.every('summary.epoch', epoch, summary_interval):
            self._save_summary(epoch)
        floor_epoch = math.floor(epoch)
        cp_interval = system.checkpoint.get('save.interval', 0)
        if self.change.every('checkpoint.epoch', floor_epoch, cp_interval):
            log.info(
                'Saving checkpoint at epoch {}...'.format(epoch), update=True)
            with log.demote():
                self.save_checkpoint(floor_epoch)
            self._checkpoint_epoch = floor_epoch
        if system.max_epochs and floor_epoch >= system.max_epochs:
            log.info(
                'Maximum epoch count {} reached.'.format(system.max_epochs))
            if self._checkpoint_epoch and floor_epoch > self._checkpoint_epoch:
                log.info('Saving final checkpoint...')
                self.save_checkpoint(floor_epoch)
            return False
        return True

    def train(self):
        # final debug outputs
        lr = self.run(self.learning_rate)
        log.info('Training start with a learning rate {}.'.format(lr))
        try:
            # train iterations
            while self._iteration():
                pass
        except KeyboardInterrupt:
            log.info('Stopped.')
            save = self.config.system.checkpoint.get('save', {})
            if save:
                countdown = save.get('countdown', 0)
                if log.countdown('Saving checkpoint', countdown):
                    self.save_checkpoint('latest')
