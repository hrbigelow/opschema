from schema import flib

def init_schema(op):
    op.add_index('b', 'batch', (1, 1))
    op.add_index('i', 'input spatial', (1, 3))
    op.add_index('o', 'output spatial', 'i')
    op.add_index('c', 'channel', (1, 1))
    op.add_index('k', 'ksize', 'i')
    op.add_index('s', 'strides', 'i')

    formats = {
            'NCW': (0, 1),
            'NCHW': (0, 2),
            'NCDHW': (0, 3),
            'NWC': (1, 1),
            'NHWC': (1, 2),
            'NDHWC': (1, 3),
            None: (1, None),
            }

    op.arg_layout('data_format', formats, 'i')
    op.arg_tensor('input', 'bci', 'bic')
    op.arg_shape_bcast_list('ksize', 'k')
    op.arg_shape_bcast_list('strides', 's')
    op.arg_option('padding', ('VALID', 'SAME'))
    op.arg_unchecked('name')

    op.valid_dtypes('input', ('bfloat16', 'float',))

    op.exclude_combos('input', ('float64', 'bfloat16'), 'i', 3)

    def odims(i, k, s, padding):
        if padding == 'VALID':
            tmp = i - k + 1
            out = flib.ceildiv(tmp, s)
        else:
            out = flib.ceildiv(i, s)
        return out

    def odims_templ(i, k, s, padding):
        if padding == 'VALID':
            tem = f'ceil(({i} - {k} + 1) / {s}) (VALID padding)'
        else:
            tem = f'ceil({i} / {s}) (SAME padding)'
        return tem

    op.add_index_generator('k', flib.gen_range, 'k', 1, 5)
    op.add_index_generator('s', flib.gen_range, 's', 1, 5)

    op.computed_index('o', odims, odims_templ, 'iks', 0, 'padding')

    op.return_tensor('bco', 'boc')

    
