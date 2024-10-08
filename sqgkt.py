import torch
from torch.nn import Module, Embedding, Linear, ModuleList, Dropout, LSTMCell
from params import DEVICE
from scipy import sparse


class sqgkt(Module):

    def __init__(self, num_question, num_skill, q_neighbors, s_neighbors, qs_table,num_user, u_neighbors, q_neighbors_2, uq_table, agg_hops=3, emb_dim=100,
                 dropout=(0.2, 0.4), hard_recap=True, rank_k=10):
        super(sqgkt, self).__init__()
        self.model_name = "sqgkt"
        self.num_question = num_question
        self.num_skill = num_skill
        self.q_neighbors = q_neighbors
        self.s_neighbors = s_neighbors

        self.num_user = num_user
        # self.num_question = num_question
        self.u_neighbors = u_neighbors
        self.q_neighbors_2 = q_neighbors_2

        self.agg_hops = agg_hops
        self.qs_table = qs_table
        self.uq_table = uq_table

        self.emb_dim = emb_dim
        self.hard_recap = hard_recap
        self.rank_k = rank_k

        self.emb_table_question = Embedding(num_question, emb_dim)
        self.emb_table_skill = Embedding(num_skill, emb_dim)
        self.emb_table_user = Embedding(num_user, emb_dim)
        self.emb_table_question_2 = Embedding(num_question, emb_dim)

        self.emb_table_response = Embedding(2, emb_dim)


        self.lstm_cell = LSTMCell(input_size=emb_dim * 2, hidden_size=emb_dim)
        self.mlps4agg = ModuleList(Linear(emb_dim, emb_dim) for _ in range(agg_hops))
        self.MLP_AGG_last = Linear(emb_dim, emb_dim)
        self.dropout_lstm = Dropout(dropout[0])

        self.dropout_gnn = Dropout(dropout[1])
        self.MLP_query = Linear(emb_dim, emb_dim)
        self.MLP_key = Linear(emb_dim, emb_dim)

        self.MLP_W = Linear(2 * emb_dim, 1)

    def forward(self, user,question, response, mask):
        # question: [batch_size, seq_len]
        # response: [batch_size, 1]
        # mask: [batch_size, seq_len]

        batch_size, seq_len = question.shape
        q_neighbor_size, s_neighbor_size = self.q_neighbors.shape[1], self.s_neighbors.shape[1]
        u_neighbor_size, q_neighbor_size_2 = self.u_neighbors.shape[1], self.q_neighbors_2.shape[1]
        h1_pre = torch.nn.init.xavier_uniform_(torch.zeros(self.emb_dim, device=DEVICE).repeat(batch_size, 1))
        h2_pre = torch.nn.init.xavier_uniform_(torch.zeros(self.emb_dim, device=DEVICE).repeat(batch_size, 1))
        state_history = torch.zeros(batch_size, seq_len, self.emb_dim, device=DEVICE)
        y_hat = torch.zeros(batch_size, seq_len, device=DEVICE)
        uq_table = self.uq_table
        for t in range(seq_len - 1):
            user_t = user[:, t]
            question_t = question[:, t]
            response_t = response[:, t]
            mask_t = torch.eq(mask[:, t], torch.tensor(1))
            emb_response_t = self.emb_table_response(response_t) # [batch_size, emb_dim]
            # GCN embedding
            node_neighbors = [question_t[mask_t]]
            _batch_size = len(node_neighbors[0])
            for i in range(self.agg_hops):
                nodes_current = node_neighbors[-1].reshape(-1)
                nodes_current = nodes_current.reshape(-1)
                neighbor_shape = [_batch_size] + [(q_neighbor_size if j % 2 == 0 else s_neighbor_size) for j in range(i + 1)]

                if i % 2 == 0:
                    node_neighbors.append(self.q_neighbors[nodes_current].reshape(neighbor_shape))
                else:
                    node_neighbors.append(self.s_neighbors[nodes_current].reshape(neighbor_shape))
            emb_node_neighbor = []
            for i, nodes in enumerate(node_neighbors):
                if i % 2 == 0:
                    emb_node_neighbor.append(self.emb_table_question(nodes))
                else:
                    emb_node_neighbor.append(self.emb_table_skill(nodes))

            node_neighbors_2 = [user_t[mask_t]]
            _batch_size_2 = len(node_neighbors_2[0])
            for i in range(self.agg_hops):
                nodes_current_2 = node_neighbors_2[-1].reshape(-1)
                # nodes_current = nodes_current.reshape(-1)
                neighbor_shape_2 = [_batch_size] + [(u_neighbor_size if j % 2 == 0 else q_neighbor_size_2) for j in
                                                  range(i + 1)]

                if i % 2 == 0:
                    node_neighbors_2.append(self.u_neighbors[nodes_current_2].reshape(neighbor_shape_2))
                else:
                    node_neighbors_2.append(self.q_neighbors_2[nodes_current_2].reshape(neighbor_shape_2))
            emb_node_neighbor_2 = []
            for i, nodes in enumerate(node_neighbors_2):
                if i % 2 == 0:
                    emb_node_neighbor_2.append(self.emb_table_user(nodes))
                else:
                    emb_node_neighbor_2.append(self.emb_table_question_2(nodes))
            emb0_question_t = self.aggregate(emb_node_neighbor)
            emb_question_t = torch.zeros(batch_size, self.emb_dim, device=DEVICE)
            emb_question_t[mask_t] = emb0_question_t
            emb_question_t[~mask_t] = self.emb_table_question(question_t[~mask_t])

            emb0_question_t_2 = self.aggregate_uq(emb_node_neighbor_2)

            emb_question_t_2 = torch.zeros(batch_size, self.emb_dim, device=DEVICE)
            emb_question_t_2[mask_t] = emb0_question_t_2
            emb_question_t_2[~mask_t] = self.emb_table_question_2(question_t[~mask_t])

            emb_question_t = emb_question_t + emb_question_t_2
            # LSTM updates knowledge status
            lstm_input = torch.cat((emb_question_t, emb_response_t), dim=1) # [batch_size, emb_dim * 2]
            lstm_output = self.dropout_lstm(self.lstm_cell(lstm_input)[0]) # [batch_size, emb_dim]

            q_next = question[:, t + 1]
            skills_related = self.qs_table[q_next]
            skills_related_list = []
            max_num_skill = 1
            for i in range(batch_size):
                skills_index = torch.nonzero(skills_related[i]).squeeze()
                if len(skills_index.shape) == 0:
                    skills_related_list.append(torch.unsqueeze(self.emb_table_skill(skills_index), dim=0))
                else:
                    skills_related_list.append(self.emb_table_skill(skills_index))
                    if skills_index.shape[0] > max_num_skill:
                        max_num_skill = skills_index.shape[0]


            emb_q_next = self.emb_table_question(q_next)
            qs_concat = torch.zeros(batch_size, max_num_skill + 1, self.emb_dim).to(DEVICE)
            for i, emb_skills in enumerate(skills_related_list):
                num_qs = 1 + emb_skills.shape[0]
                emb_next = torch.unsqueeze(emb_q_next[i], dim=0)
                qs_concat[i, 0 : num_qs] = torch.cat((emb_next, emb_skills), dim=0)

            if t == 0:
                y_hat[:, 0] = 0.5
                y_hat[:, 1] = self.predict(qs_concat, torch.unsqueeze(lstm_output, dim=1))
                continue
            if self.hard_recap:
                history_time = self.recap_hard(q_next, question[:, 0:t])
                selected_states = []
                max_num_states = 1
                for row, selected_time in enumerate(history_time):
                    current_state = torch.unsqueeze(lstm_output[row], dim=0)
                    if len(selected_time) == 0:
                        selected_states.append(current_state)
                    else:
                        selected_state = state_history[row, torch.tensor(selected_time, dtype=torch.int64)]
                        selected_states.append(torch.cat((current_state, selected_state), dim=0))
                        if (selected_state.shape[0] + 1) > max_num_states:
                            max_num_states = selected_state.shape[0] + 1
                current_history_state = torch.zeros(batch_size, max_num_states, self.emb_dim).to(DEVICE)

                for b, c_h_state in enumerate(selected_states):
                    num_states = c_h_state.shape[0]
                    current_history_state[b, 0 : num_states] = c_h_state
            else:
                current_state = lstm_output.unsqueeze(dim=1)
                if t <= self.rank_k:
                    current_history_state = torch.cat((current_state, state_history[:, 0:t]), dim=1)
                else:
                    Q = self.emb_table_question(q_next).clone().detach().unsqueeze(dim=-1)
                    K = self.emb_table_question(question[:, 0:t]).clone().detach()
                    product_score = torch.bmm(K, Q).squeeze(dim=-1)
                    _, indices = torch.topk(product_score, k=self.rank_k, dim=1)
                    select_history = torch.cat(tuple(state_history[i][indices[i]].unsqueeze(dim=0)
                                                     for i in range(batch_size)), dim=0)
                    current_history_state = torch.cat((current_state, select_history), dim=1)
            y_hat[:, t + 1] = self.predict(qs_concat, current_history_state)
            state_history[:, t] = lstm_output
        return y_hat

    def aggregate(self, emb_node_neighbor):
        for i in range(self.agg_hops):
            for j in range(self.agg_hops - i):
                emb_node_neighbor[j] = self.sum_aggregate(emb_node_neighbor[j], emb_node_neighbor[j + 1], j)
        return torch.tanh(self.MLP_AGG_last(emb_node_neighbor[0]))

    def sum_aggregate(self, emb_self, emb_neighbor, hop):
        emb_sum_neighbor = torch.mean(emb_neighbor, dim=-2)
        emb_sum = (emb_sum_neighbor + emb_self)
        return torch.tanh(self.dropout_gnn(self.mlps4agg[hop](emb_sum)))

    def aggregate_uq(self, emb_node_neighbor):
        for i in range(self.agg_hops):
            for j in range(self.agg_hops - i):
                emb_node_neighbor[j] = self.sum_aggregate_uq(emb_node_neighbor[j], emb_node_neighbor[j + 1], j)
        return torch.tanh(self.MLP_AGG_last(emb_node_neighbor[0]))

    def sum_aggregate_uq(self, emb_self, emb_neighbor, hop):
        num_nodes = emb_self.size(0)
        embedding_dim = emb_self.size(1)
        weighted_emb_neighbor_sum = torch.zeros_like(emb_self)
        for i in range(num_nodes):
            neighbor_embs = emb_neighbor[i]
            node_weights = self.uq_table[i, :neighbor_embs.size(0)]
            weighted_neighbor_embs = neighbor_embs * node_weights.unsqueeze(-1)
            weighted_emb_neighbor_sum[i] = torch.mean(weighted_neighbor_embs, dim=0)
        emb_sum = emb_self + weighted_emb_neighbor_sum
        return torch.tanh(self.dropout_gnn(self.mlps4agg[hop](emb_sum)))

    def recap_hard(self, q_next, q_history):
        batch_size = q_next.shape[0]
        q_neighbor_size, s_neighbor_size = self.q_neighbors.shape[1], self.s_neighbors.shape[1]
        q_next = q_next.reshape(-1)
        skill_related = self.q_neighbors[q_next].reshape((batch_size, q_neighbor_size)).reshape(-1)
        q_related = self.s_neighbors[skill_related].reshape((batch_size, q_neighbor_size * s_neighbor_size)).tolist()
        time_select = [[] for _ in range(batch_size)]
        for row in range(batch_size):
            key = q_history[row].tolist()
            query = q_related[row]
            for t, k in enumerate(key):
                if k in query:
                    time_select[row].append(t)
        return time_select

    def recap_soft(self, rank_k=10):
        pass

    def predict(self, qs_concat, current_history_state):
        output_g = torch.bmm(qs_concat, torch.transpose(current_history_state, 1, 2))
        num_qs, num_state = qs_concat.shape[1], current_history_state.shape[1]
        states = torch.unsqueeze(current_history_state, dim=1)
        states = states.repeat(1, num_qs, 1, 1)
        qs_concat2 = torch.unsqueeze(qs_concat, dim=2)
        qs_concat2 = qs_concat2.repeat(1, 1, num_state, 1)
        K = torch.tanh(self.MLP_query(states))  # [batch_size, num_qs, num_state, dim_emb]
        Q = torch.tanh(self.MLP_key(qs_concat2))  # [batch_size, num_qs, num_state, dim_emb]
        tmp = self.MLP_W(torch.cat((Q, K), dim=-1))  # [batch_size, num_qs, num_state, 1]
        tmp = torch.squeeze(tmp, dim=-1)  # [batch_size, num_qs, num_state]
        alpha = torch.softmax(tmp, dim=2)  # [batch_size, num_qs, num_state]
        p = torch.sum(torch.sum(alpha * output_g, dim=1), dim=1)  # [batch_size, 1]
        result = torch.sigmoid(torch.squeeze(p, dim=-1)) # [batch_size, ]
        return result