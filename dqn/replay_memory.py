import os
import random
import numpy as np


class GANReplayMemory(object):
    """Dyna-Qのような学習に必要"""

    def __init__(self, config):
        self.cnn_format = config.cnn_format
        self.memory_size = config.gan_memory_size
        self.history_length = config.history_length
        self.dims = (config.screen_height, config.screen_width)
        self.batch_size = config.batch_size
        self.count = 0
        self.current = 0

        self.states = np.empty(
            (self.memory_size, self.history_length) + self.dims, dtype=np.uint8)
        self.actions = np.empty([self.memory_size], dtype=np.uint8)
        self.rewards = np.empty([self.memory_size], dtype=np.integer)
        self.terminals = np.full([self.batch_size], False)
        # pre-allocate prestates for minibatch
        self.prestates = np.empty(
            (self.batch_size, self.history_length) + self.dims, dtype=np.uint8)

    def add_batch(self, frames, act, rew):
        self.states[self.current, ...] = frames
        self.actions[self.current] = act
        self.rewards[self.current] = rew

        self.count = max(self.count, self.current + 1)
        self.current = (self.current + 1) % self.memory_size

    def can_sample(self, batch_size):
        """Returns true if `batch_size` different transitions can be sampled from the buffer."""
        return batch_size + 1 <= self.count

    def sample(self):
            # memory must include poststate, prestate and history
        assert self.count > self.history_length
        # sample random indexes
        indexes = np.random.randint(
            0, self.count - 1, (self.batch_size))

        self.prestates = self.states[indexes]
        actions = self.actions[indexes]
        rewards = self.rewards[indexes]

        if self.cnn_format == 'NHWC':
            return np.transpose(self.prestates, (0, 2, 3, 1)), actions, rewards, self.terminals
        else:
            return self.prestates, actions, rewards, self.terminals


class ReplayMemory:
    def __init__(self, config, model_dir):
        self.model_dir = model_dir

        self.cnn_format = config.cnn_format
        self.memory_size = config.memory_size
        self.actions = np.empty(self.memory_size, dtype=np.uint8)
        self.rewards = np.empty(self.memory_size, dtype=np.integer)
        self.screens = np.empty(
            (self.memory_size, config.screen_height, config.screen_width), dtype=np.uint8)
        self.terminals = np.empty(self.memory_size, dtype=np.bool)
        self.history_length = config.history_length
        self.dims = (config.screen_height, config.screen_width)
        self.batch_size = config.batch_size
        self.gan_batch_size = config.gan_batch_size
        self.rp_batch_size = config.rp_batch_size
        self.nonzero_batch_size = config.nonzero_batch_size
        self.lookahead = config.lookahead
        self.count = 0
        self.current = 0
        # reward predictor
        self.nonzero_rewards = []
        self.overwrite_index = None

        # pre-allocate prestates and poststates for minibatch
        self.prestates = np.empty(
            (self.batch_size, self.history_length) + self.dims, dtype=np.uint8)
        self.poststates = np.empty(
            (self.batch_size, self.history_length) + self.dims, dtype=np.uint8)
        self.gan_states = np.empty(
            (self.gan_batch_size, self.history_length + self.lookahead) + self.dims, dtype=np.uint8)
        self.reward_states = np.empty(
            (self.rp_batch_size, self.history_length) + self.dims, dtype=np.uint8)
        self.nonzero_states = np.empty(
            (self.nonzero_batch_size, self.history_length) + self.dims, dtype=np.uint8)

    def add(self, screen, reward, action, terminal):
        assert screen.shape == self.dims
        # NB! screen is post-state, after action and reward
        self.actions[self.current] = action
        self.rewards[self.current] = reward
        self.screens[self.current, ...] = screen
        self.terminals[self.current] = terminal

        if(self.overwrite_index != None and self.current == self.nonzero_rewards[self.overwrite_index]):
            self.nonzero_rewards.pop(self.overwrite_index)
            if self.overwrite_index >= len(self.nonzero_rewards):
                self.overwrite_index = None

        if (self.current + 1) >= self.memory_size and len(self.nonzero_rewards):
            self.overwrite_index = 0

        if (reward != 0):
            if self.overwrite_index == None:
                self.nonzero_rewards.append(self.current)
            else:
                self.nonzero_rewards.insert(self.overwrite_index, self.current)
                self.overwrite_index += 1

        self.count = max(self.count, self.current + 1)
        self.current = (self.current + 1) % self.memory_size

    def getState(self, index, lookahead=0):
        assert self.count > 0, "replay memory is empty, use at least --random_steps 1"
        # normalize index to expected range, allows negative indexes
        index = index % self.count
        # if is not in the beginning of matrix
        if index >= self.history_length - 1:
            # use faster slicing
            return self.screens[(index - (self.history_length - 1)):(index + 1 + lookahead), ...]
        else:
            # otherwise normalize indexes and use slower list based access
            indexes = [(index - i) %
                       self.count for i in reversed(range(self.history_length + lookahead))]
            return self.screens[indexes, ...]

    def rollout_state_action(self, num_rollout=4):
        index = 0
        while True:
            # sample one index (ignore states wraping over
            index = random.randint(self.history_length,
                                   self.count - num_rollout - 1)
            # if wraps over current pointer, then get new one
            if index + (num_rollout - 1) >= self.current and index - self.history_length < self.current:
                continue
            # if wraps over episode end, then get new one
            # NB! poststate (last screen) can be terminal state!
            if self.terminals[(index - self.history_length):index + (num_rollout - 1)].any():
                continue
            # otherwise use this index
            break
        state = self.getState(index - 1, num_rollout)
        action = self.actions[index:index+num_rollout]
        return state, action

    def sample(self):
        # memory must include poststate, prestate and history
        assert self.count > self.history_length
        # sample random indexes
        indexes = []
        while len(indexes) < self.batch_size:
            # find random index
            while True:
                # sample one index (ignore states wraping over
                index = random.randint(self.history_length, self.count - 1)
                # if wraps over current pointer, then get new one
                if index >= self.current and index - self.history_length < self.current:
                    continue
                # if wraps over episode end, then get new one
                # NB! poststate (last screen) can be terminal state!
                if self.terminals[(index - self.history_length):index].any():
                    continue
                # otherwise use this index
                break

            # NB! having index first is fastest in C-order matrices
            self.prestates[len(indexes), ...] = self.getState(index - 1)
            self.poststates[len(indexes), ...] = self.getState(index)
            indexes.append(index)

        actions = self.actions[indexes]
        rewards = self.rewards[indexes]
        terminals = self.terminals[indexes]

        if self.cnn_format == 'NHWC':
            return np.transpose(self.prestates, (0, 2, 3, 1)), actions, \
                rewards, np.transpose(self.poststates, (0, 2, 3, 1)), terminals
        else:
            return self.prestates, actions, rewards, self.poststates, terminals

    def GAN_sample(self):
        assert self.count > self.gan_batch_size

        indexes = []
        while len(indexes) < self.gan_batch_size:
            # find random index
            while True:
                # sample one index (ignore states wraping over
                # index = random.randint(
                #     self.history_length, self.count - (1 + (self.lookahead - 1)))
                # if self.count < 60000:
                #     index = random.randint(
                #         self.history_length, self.count - (1 + (self.lookahead - 1)))
                # else:
                index = (self.current-random.randint(self.lookahead+self.history_length, 60000)) % (
                    self.count-2*self.lookahead-2*self.history_length-1)+self.lookahead+self.history_length

                # if wraps over current pointer, then get new one
                if index + (self.lookahead - 1) >= self.current and index - self.history_length < self.current:
                    continue
                # if wraps over episode end, then get new one
                # NB! poststate (last screen) can be terminal state!
                if self.terminals[(index - self.history_length):index + (self.lookahead - 1)].any():
                    continue
                # otherwise use this index
                break

            # NB! having index first is fastest in C-order matrices
            self.gan_states[len(indexes), ...] = self.getState(
                index - 1, self.lookahead)
            indexes.append(index)

        if self.lookahead == 1:
            actions = np.expand_dims(self.actions[indexes], axis=1)
        else:
            actions = [self.actions[i:i+self.lookahead] for i in indexes]

        if self.cnn_format == 'NHWC':
            return np.transpose(self.gan_states[:, :self.history_length, ...], (0, 2, 3, 1)), actions, np.transpose(self.gan_states[:, self.history_length:, ...], (0, 2, 3, 1))
        else:
            return self.gan_states[:, :self.history_length, ...], actions, self.gan_states[:, self.history_length:, ...]

    def reward_sample(self, batch_size, nonzero=False):
        assert self.count > batch_size

        indexes = []
        missing_context = 0
        missing_context_index = 0
        missing_context_indexes = []
        while len(indexes) < batch_size:
            # find random index
            while True:
                # sample one index (ignore states wraping over
                if nonzero == True and (len(self.nonzero_rewards) > 0):
                    nonzero_index = np.random.choice(
                        self.nonzero_rewards) - random.randint(0, self.lookahead)
                    while nonzero_index % (self.count-self.lookahead-2) != nonzero_index:
                        nonzero_index = np.random.choice(
                            self.nonzero_rewards) - random.randint(0, self.lookahead)
                    index = nonzero_index
                else:
                    if self.count < 60000:
                        index = random.randint(
                            self.history_length + self.lookahead, self.count - (1 + self.lookahead))
                    else:
                        index = (self.current-random.randint(self.history_length,
                                                             60000)) % (self.count-self.lookahead-self.history_length)
                    #     if 0 > index:
                    #         index += self.count
                    # if nonzero == False and ((index in self.nonzero_rewards) or (index + 1 in self.nonzero_rewards)):
                    #     continue

                # if wraps over current pointer, then get new one
                if index - 1 >= self.current and index - self.history_length < self.current:
                    continue
                # if wraps over episode end, then get new one
                # NB! poststate (last screen) can be terminal state!
                if self.terminals[(index - self.history_length):index - 1].any():
                #     missing_context_index = np.where(
                #         self.terminals[(index - self.history_length):index - 1] == True)
                #     missing_context = self.history_length - index - missing_context_index
                #     break
                    continue
                # otherwise use this index
                break

            # NB! having index first is fastest in C-order matrices
            if missing_context_index > 0:
                buf_state = self.getState(index - 1)
                buf_state[: missing_context] = np.repeat(
                    buf_state[missing_context + 1: ...], missing_context, 0)
                self.reward_states[len(indexes), ...] = buf_state
                missing_context_indexes.append(index)
            else:
                if nonzero == False:
                    self.reward_states[len(indexes), ...] = self.getState(
                        index - 1)
                else:
                    self.nonzero_states[len(indexes), ...] = self.getState(
                        index - 1)
            indexes.append(index)

        actions = [self.actions[i:i+self.lookahead+1] for i in indexes]
        rewards = [self.rewards[i:i+self.lookahead+1] for i in indexes]

        if self.cnn_format == 'NHWC':
            if nonzero == False:
                return np.transpose(self.reward_states, (0, 2, 3, 1)), actions, rewards
            else:
                return np.transpose(self.nonzero_states, (0, 2, 3, 1)), actions, rewards
        else:
            if nonzero == False:
                return self.reward_states, actions, rewards
            else:
                return self.nonzero_states, actions, rewards

    def can_sample(self, batch_size):
        return batch_size + 1 <= self.count


# def test_dqn_replay_memory():
#     class config():
#         cnn_format = 'NCHW'
#         memory_size = 100
#         batch_size = 5
#         gan_batch_size = 5
#         rp_batch_size = 5
#         lookahead = 1
#         history_length = 4
#         screen_height = 1
#         screen_width = 1
#     config = config()
#     model_dir = ""
#     test_memory = ReplayMemory(config, model_dir)
#     test_data = np.arange(1, 101)
#     test_memory.actions[0: 100] = test_data
#     test_memory.rewards[0: 100] = test_data
#     test_memory.screens[0: 100, ...] = np.repeat(
#         test_data, 1**2).reshape([100, 1, 1])
#     # print(test_memory.rewards)
#     # print(test_memory.actions)
#     test_memory.count = 100
#     test_memory.current = random.randint(0, 100)

#     pre, act, rew, post, _ = test_memory.sample()
#     pre_index = pre[]

#     assert pre == np.arange(
#         pre_index - 4, pre_index), "{},{}".format(pre, np.arange(pre_index - 4, pre_index))
#     assert act == pre_index + 1
#     assert rew == pre_index + 1
#     assert post == pre_index


# if __name__ == "__main__":

#     test_dqn_replay_memory()
