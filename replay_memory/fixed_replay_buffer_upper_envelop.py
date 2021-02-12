# coding=utf-8
# Copyright 2020 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Logged Replay Buffer."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
from concurrent import futures
from dopamine.replay_memory import circular_replay_buffer_upper_envelop

import numpy as np
import tensorflow as tf

gfile = tf.gfile

STORE_FILENAME_PREFIX = circular_replay_buffer_upper_envelop.STORE_FILENAME_PREFIX


class FixedReplayBufferUpperEnvelop(object):
    """Object composed of a list of OutofGraphReplayBuffers."""

    def __init__(self, data_dir, replay_suffix, data_set_mode, train_mode, *args,
                 **kwargs):  # pylint: disable=keyword-arg-before-vararg
        """Initialize the FixedReplayBuffer class.

        Args:
          data_dir: str, log Directory from which to load the replay buffer.
          replay_suffix: int, If not None, then only load the replay buffer
            corresponding to the specific suffix in data directory.
          *args: Arbitrary extra arguments.
          **kwargs: Arbitrary keyword arguments.
        """
        self._args = args
        self._kwargs = kwargs
        self._data_dir = data_dir
        self._loaded_buffers = False
        self._train_mode = train_mode
        self.add_count = np.array(0)
        self._replay_suffix = replay_suffix
        self._data_set_mode = data_set_mode
        assert self._data_set_mode in ['ALL', 'POOR', 'HIGH', 'MEDIUM'], \
            "The data set mode only supported in {'ALL', 'POOR', 'HIGH', 'MEDIUM'}"

        if self._data_set_mode == 'MEDIUM':
            self._random_twenty_percent_suffixes = None

        while not self._loaded_buffers:
            if replay_suffix:
                assert replay_suffix >= 0, 'Please pass a non-negative replay suffix'
                self.load_single_buffer(replay_suffix)
            else:
                self._load_replay_buffers(num_buffers=1, with_return=False)

    def load_single_buffer(self, suffix):
        """Load a single replay buffer."""
        replay_buffer = self._load_buffer(suffix)
        if replay_buffer is not None:
            self._replay_buffers = [replay_buffer]
            self.add_count = replay_buffer.add_count
            self._num_replay_buffers = 1
            self._loaded_buffers = True

    def _load_buffer(self, suffix, with_return=False, with_bc=False, with_estimated_return=False,
                     dir=None, border=None):
        """Loads a OutOfGraphReplayBuffer replay buffer."""
        if not dir:
            data_dir = self._data_dir
        else:
            data_dir = dir

        try:
            # pytype: disable=attribute-error
            replay_buffer = circular_replay_buffer_upper_envelop.OutOfGraphReplayBuffer(
                *self._args, **self._kwargs)
            replay_buffer.load(data_dir, suffix, with_return=with_return, with_bc=with_bc,
                               with_estimated_return=with_estimated_return,
                               border=border, train_mode=self._train_mode)
            tf.logging.info('Loaded replay buffer ckpt {} from {}'.format(
                suffix, self._data_dir))
            # pytype: enable=attribute-error
            return replay_buffer
        except tf.errors.NotFoundError:
            return None

    def _load_replay_buffers(self, num_buffers=None, with_return=True, with_bc=False,
                             with_estimated_return=False,
                             border=None):
        """Loads multiple checkpoints into a list of replay buffers."""
        data_dir = self._data_dir
        min_ckpt_counter = 6

        if not self._loaded_buffers:  # pytype: disable=attribute-error
            ckpts = gfile.ListDirectory(data_dir)  # pytype: disable=attribute-error
            # Assumes that the checkpoints are saved in a format CKPT_NAME.{SUFFIX}.gz
            ckpt_counters = collections.Counter(
                [name.split('.')[-2] for name in ckpts])
            # Should contain the files for add_count, action, observation, reward,
            # terminal and invalid_range
            ckpt_suffixes = [int(x) for x in ckpt_counters if ckpt_counters[x] >= min_ckpt_counter]
            ckpt_suffixes.sort()
            ckpt_suffixes = [str(x) for x in ckpt_suffixes]
            num_suffixes = len(ckpt_suffixes)

            my_chosen_ckpt_suffixes = None

            # choose replay buffers based on the data set mode
            if num_buffers is not None:
                if self._data_set_mode == 'ALL':
                    my_chosen_ckpt_suffixes = ckpt_suffixes
                    ckpt_suffixes = np.random.choice(
                        ckpt_suffixes, num_buffers, replace=False)
                elif self._data_set_mode == 'POOR':
                    my_chosen_ckpt_suffixes = ckpt_suffixes[0: int(num_suffixes * 0.2)]
                    ckpt_suffixes = np.random.choice(
                        my_chosen_ckpt_suffixes, num_buffers, replace=False)
                elif self._data_set_mode == 'HIGH':
                    my_chosen_ckpt_suffixes = ckpt_suffixes[int(num_suffixes * 0.8):]
                    ckpt_suffixes = np.random.choice(
                        my_chosen_ckpt_suffixes, num_buffers, replace=False)
                elif self._data_set_mode == 'MEDIUM':
                    if self._random_twenty_percent_suffixes is None:
                        self._random_twenty_percent_suffixes = np.random.choice(
                            ckpt_suffixes, int(num_suffixes * 0.2), replace=False
                        )
                    ckpt_suffixes = np.random.choice(
                        self._random_twenty_percent_suffixes, num_buffers, replace=False)
                    my_chosen_ckpt_suffixes = self._random_twenty_percent_suffixes
                else:
                    raise NotImplementedError("This kind of data set mode is not supported.")

            self.my_chosen_ckpt_suffixes = my_chosen_ckpt_suffixes

            print("ckpt_suffixes {} has been chosen".format(ckpt_suffixes))
            self._replay_buffers = []
            # Load the replay buffers in parallel
            with futures.ThreadPoolExecutor(
                    max_workers=num_buffers) as thread_pool_executor:
                replay_futures = [thread_pool_executor.submit(
                    self._load_buffer, suffix, with_return, with_bc, with_estimated_return,
                    data_dir, border) for suffix in ckpt_suffixes]
            for f in replay_futures:
                replay_buffer = f.result()
                if replay_buffer is not None:
                    self._replay_buffers.append(replay_buffer)
                    self.add_count = max(replay_buffer.add_count, self.add_count)
            self._num_replay_buffers = len(self._replay_buffers)
            if self._num_replay_buffers:
                self._loaded_buffers = True

    def get_transition_elements(self, mode=None):
        return self._replay_buffers[0].get_transition_elements(mode=mode)

    def sample_transition_batch(self, batch_size=None, indices=None):
        buffer_index = np.random.randint(self._num_replay_buffers)
        return self._replay_buffers[buffer_index].sample_transition_batch(
            batch_size=batch_size, indices=indices)

    def sample_transition_batch_bc(self, batch_size=None, indices=None):
        buffer_index = np.random.randint(self._num_replay_buffers)
        return self._replay_buffers[buffer_index].sample_transition_batch(
            batch_size=batch_size, indices=indices, mode='bc')

    def sample_transition_batch_bcq(self, batch_size=None, indices=None):
        buffer_index = np.random.randint(self._num_replay_buffers)
        return self._replay_buffers[buffer_index].sample_transition_batch(
            batch_size=batch_size, indices=indices, mode='bcq')

    def sample_transition_batch_ue(self, batch_size=None, indices=None):
        buffer_index = np.random.randint(self._num_replay_buffers)
        return self._replay_buffers[buffer_index].sample_transition_batch_ue(
            batch_size=batch_size, indices=indices)

    def load(self, *args, **kwargs):  # pylint: disable=unused-argument
        pass

    def reload_buffer(self, num_buffers=None, with_return=False, with_bc=False, with_estimated_return=False,
                      **kwargs):
        self._loaded_buffers = False
        self._load_replay_buffers(num_buffers, with_return, with_bc, with_estimated_return, **kwargs)

    def save(self, *args, **kwargs):  # pylint: disable=unused-argument
        pass

    def add(self, *args, **kwargs):  # pylint: disable=unused-argument
        pass


class WrappedFixedReplayBuffer(circular_replay_buffer_upper_envelop.WrappedReplayBuffer):
    """Wrapper of OutOfGraphReplayBuffer with an in graph sampling mechanism."""

    def __init__(self,
                 data_dir,
                 replay_suffix,
                 observation_shape,
                 stack_size,
                 use_staging=True,
                 replay_capacity=1000000,
                 batch_size=32,
                 update_horizon=1,
                 gamma=0.99,
                 wrapped_memory=None,
                 max_sample_attempts=1000,
                 extra_storage_types=None,
                 observation_dtype=np.uint8,
                 action_shape=(),
                 action_dtype=np.int32,
                 reward_shape=(),
                 reward_dtype=np.float32,
                 data_set_mode='ALL',
                 train_mode=None,
                 border=None,
                 ):
        """Initializes WrappedFixedReplayBuffer."""

        memory = FixedReplayBufferUpperEnvelop(
            data_dir, replay_suffix, data_set_mode, train_mode,
            observation_shape, stack_size, replay_capacity,
            batch_size, update_horizon, gamma, max_sample_attempts,
            extra_storage_types=extra_storage_types,
            observation_dtype=observation_dtype,
            border=border,
        )

        super(WrappedFixedReplayBuffer, self).__init__(
            observation_shape,
            stack_size,
            use_staging=use_staging,
            replay_capacity=replay_capacity,
            batch_size=batch_size,
            update_horizon=update_horizon,
            gamma=gamma,
            wrapped_memory=memory,
            max_sample_attempts=max_sample_attempts,
            extra_storage_types=extra_storage_types,
            observation_dtype=observation_dtype,
            action_shape=action_shape,
            action_dtype=action_dtype,
            reward_shape=reward_shape,
            reward_dtype=reward_dtype,
            train_mode=train_mode
        )
