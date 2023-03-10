# -*- encoding: utf-8 -*-

import math
import paddle
from paddle import nn, Tensor
import paddle.nn.functional as F
from paddle.nn import TransformerEncoder, TransformerEncoderLayer
from paddle.optimizer.lr import LambdaDecay
from args import config
global_dtype = paddle.get_default_dtype()

def get_linear_schedule_with_warmup(learning_rate: float,
                                    num_warmup_steps,
                                    num_training_steps,
                                    last_epoch=-1):
    """
    Create a schedule with a learning rate that decreases linearly from the initial lr set in the optimizer to 0, after
    a warmup period during which it increases linearly from 0 to the initial lr set in the optimizer.
    Args:
        learning_rate (float)
            The initial learning rate. It is a python float number.
        num_warmup_steps (`int`):
            The number of steps for the warmup phase.
        num_training_steps (`int`):
            The total number of training steps.
        last_epoch (`int`, *optional*, defaults to -1):
            The index of the last epoch when resuming training.
    Return:
        `paddle.optimizer.lr.LambdaDecay` with the appropriate schedule.
    """

    def lr_lambda(current_step: int):
        if current_step < num_warmup_steps:
            return float(current_step) / float(max(1, num_warmup_steps))
        return max(
            0.0,
            float(num_training_steps - current_step) /
            float(max(1, num_training_steps - num_warmup_steps)))

    return LambdaDecay(learning_rate, lr_lambda, last_epoch)

class PositionalEncoding(nn.Layer):

    def __init__(self, d_model: int, dropout: float = 0.1, max_len: int = 513):
        super().__init__()
        self.dropout = dropout

        position = paddle.arange(max_len).unsqueeze(1)
        div_term = paddle.exp(paddle.arange(0, d_model, 2) * (-math.log(10000.0) / d_model))
        pe = paddle.zeros([max_len, 1, d_model], dtype = paddle.float32)
        # import ipdb
        # ipdb.set_trace()
        pe[:, 0, 0::2] = paddle.sin(paddle.matmul(paddle.ones([max_len,1]), paddle.reshape(div_term, (1, -1))))
        pe[:, 0, 1::2] = paddle.cos(paddle.matmul(paddle.ones([max_len,1]), paddle.reshape(div_term, (1, -1))))
        self.register_buffer('pe', pe)

    def forward(self, x: Tensor) -> Tensor:
        """
        Args:
            x: Tensor, shape [seq_len, batch_size, embedding_dim]
        """
        return F.dropout(self.pe[:x.shape[0]], self.dropout)


def torch_padding_mask_to_paddle(padding_mask, n_head):
    """ ?????? padding_mask ????????? [bs, seq_len] ???numpy????????? """
    # mask = np.expand_dims(padding_mask, 1)  # [bs, 1, seq_len]
    
    mask = paddle.to_tensor(paddle.unsqueeze(padding_mask, axis = 1), dtype=paddle.float32)
    mask = paddle.to_tensor(paddle.einsum("ijk, ijl -> ikl" , mask, mask), dtype=paddle.int64) # [bs, seq_len, seq_len]
    
    paddle_mask = paddle.unsqueeze(mask, 1)
    new_shape = paddle_mask.shape[:1] + [n_head] + paddle_mask.shape[2:] 
    paddle_mask = paddle.expand(paddle_mask, new_shape) # [bs, n_head, seq_len, seq_len]
    return paddle_mask


class TransformerModel(nn.Layer):

    def __init__(self, ntoken, hidden, nhead, nlayers, dropout, mode='finetune'):
        super().__init__()
        print('Transformer is used for {}'.format(mode))
        self.ntoken = ntoken
        self.pos_encoder = PositionalEncoding(hidden)
        encoder_layers = TransformerEncoderLayer(hidden, nhead, hidden, dropout, activation='gelu')
        self.transformer_encoder = TransformerEncoder(encoder_layers, nlayers)
        self.token_encoder = nn.Embedding(ntoken, hidden)
        self.segment_encoder = nn.Embedding(2, hidden)
        self.norm_layer = nn.LayerNorm(hidden)
        self.hidden = hidden
        self.mode = mode
        self.n_head = nhead
        self.dropout = nn.Dropout(dropout)

        if mode == 'pretrain':
            self.to_logics = nn.Linear(hidden, ntoken)
            self.Display_decoder = nn.Linear(hidden,1)
            self.Dwelling_decoder = nn.Linear(hidden, 1)
            self.decoder = nn.Linear(hidden, 1)
            

            #nn.Linear(hidden, ntoken)????????
        elif mode == 'finetune':
            self.act = nn.ELU(alpha=1.0)
            self.fc1 = nn.Linear(hidden, 512)
            self.fc2 = nn.Linear(512, 256)
            self.fc3 = nn.Linear(256, 128)
            self.fc4 = nn.Linear(128, 1)

    def expand_emb(self, extened_vocab_size: int):  # len(vocab_dic)
        total_feature_dim = extened_vocab_size  #
        temp_emb = nn.Embedding(total_feature_dim, self.hidden)
        # n = len(total_feature_dim)
        self.pre_train_encoder = nn.Embedding(self.ntoken + total_feature_dim, self.hidden)
        w0 = paddle.concat(x=[self.token_encoder.weight, temp_emb.weight], axis=0)
        self.pre_train_encoder.weight.set_value(w0)
        self.to_logics = nn.Linear(self.hidden, self.ntoken)  # ntoken ++
        self.decoder = nn.Linear(self.hidden, 1)
        self.token_encoder = self.pre_train_encoder
        assert self.token_encoder.weight.shape[0] == self.ntoken + total_feature_dim
        del self.pre_train_encoder
    def expand_linear(self,extened_vocab_size:int):
        total_feature_dim = extened_vocab_size  #
        temp_Linear = nn.Linear(self.hidden,total_feature_dim)
        to_logics = nn.Linear(self.hidden, self.ntoken + total_feature_dim)

        w0 = paddle.concat(x=[self.to_logics.weight, temp_Linear.weight], axis=1)
        to_logics.weight.set_value(w0)
        self.to_logics = to_logics
    def forward(self, src, src_segment, src_padding_mask=None, mlm_label=None):
        # add label??
        # import ipdb
        # ipdb.set_trace()
        # src = src.t().contiguous().cuda()
        src = src.t()
        # src_segment = src_segment.t().contiguous().cuda()
        src_segment = src_segment.t()
        # src_padding_mask = src_padding_mask.cuda()

        # transformer input 
        pos_emb = self.pos_encoder(src)  # get position embedding
        token_emb = self.token_encoder(src) # get token embedding
        seg_emb = self.segment_encoder(src_segment)  # get position embedding
        # nontext_emb =
        #print(token_emb,pos_emb,seg_emb)
        x = token_emb + pos_emb + seg_emb
        x = self.norm_layer(x)
        x = self.dropout(x)
        x = x.transpose((1, 0 , 2))

        #TODO: add the mask
        # output = self.transformer_encoder(x, src_padding_mask)
        padding_mask = torch_padding_mask_to_paddle(src_padding_mask, self.n_head)
        # ipdb.set_trace()
        output = self.transformer_encoder(x, padding_mask).transpose([1,2,0])
        X =  output[0, :, :]  # [seqlen, bs, 1]
        X = self.dropout(X).transpose([1, 0])
        
        if self.mode == 'pretrain':  # for train
            scores = self.decoder(X)
            scores = paddle.squeeze(scores, axis=-1)
            if self.training:
                logits = self.to_logics(output.transpose([2, 0, 1]))  # shape = [bs, seq_len, num_tokens]
                pred_disply_time = self.Display_decoder(X)
                paddle.squeeze(pred_disply_time, axis=-1)
                pred_dwelling_time = self.Dwelling_decoder(X)
                paddle.squeeze(pred_dwelling_time, axis=-1)
                mlm_loss = F.cross_entropy(logits, # shape=[bs, num_class, seq_len]\
                                            paddle.to_tensor(mlm_label, paddle.int64),\
                                            ignore_index=config._PAD_ # _pad
                                #??            
                        ) 
                return scores, mlm_loss , pred_disply_time , pred_dwelling_time
            else:  
                return scores
        elif self.mode == 'finetune':
            h1 = self.act(self.fc1(X))
            h1 = self.dropout(h1)
            h2 = self.act(self.fc2(h1))
            h2 = self.dropout(h2)
            h3 = self.act(self.fc3(h2))
            h3 = self.dropout(h3)
            scores = self.fc4(h3)
            scores = paddle.squeeze(scores, axis=-1)
            return scores
