import traceback
import inspect
import sys, io, os
import re
import itertools
from random import choice
from . import predicates as pr
from . import generators as ge
from . import infer as nf
from . import report
from . import base
from . import fgraph
from .redirect import stderr_redirector
from .error import *
from .fgraph import PredNode as P, GenNode as G, FuncNode as F
from .base import ShapeKind

"""
Every API call will mutate the Generative Graph and the Predicate Graph
logically in tandem.  It should maintain the invariants:

1. Every value set produced by the Generative Graph should be valid as judged
   by the Predicate Graph

2. The set of values produced by the Generative Graph is "complete" in the
sense that it it explores every combination of dtypes, ranks, and option
arguments.  It need not explore every possible setting of dimensions or tensor
contents.

Both finished graphs must have exactly one node corresponding to each input
argument, either the arg_name:tensor node (for tensor arguments) or the
arg_name:arg node for non-tensors.
"""

class Index(object):
    """
    Represents the central shape-bearing concept.
    pri_idx is the index whose rank this one is equated to.  Or, if pri_idx ==
    idx, it means the instance itself is a primary index
    """
    def __init__(self, idx, desc, pri_idx, rank_range):
        self.idx = idx
        self.desc = desc
        self.pri_idx = pri_idx
        self.rank_range = rank_range

    def __repr__(self):
        r =  f'{type(self).__qualname__}({self.idx}): {self.desc}, '
        r += f'{self.pri_idx}, {self.rank_range}, '
        return r

    def primary(self):
        return self.idx == self.pri_idx

    def fixed_rank(self):
        return self.rank_range == (1, 1)

    def dims_range(self):
        min_dim = 0 if self.dims_min is None else self.dims_min
        max_dim = 1000000 if self.dims_max is None else self.dims_max
        return range(min_dim, max_dim + 1)

class Partial(object):
    """
    Curry extra_args at the end
    """
    def __init__(self, func, *extra_args):
        self.func = func
        self.extra_args = extra_args

    def __call__(self, *args):
        return self.func(*args, *self.extra_args)

class SchemaApi(object):
    def __init__(self, op_path):
        self.op_path = op_path
        self.index = {} # idx => Index

        # flags
        self.avail_edits = 0
        self.avail_test_edits = 0
        self.max_yield_count = 1000
        self.comp_dims_mode = True

        # error quotas
        self.dtype_err_quota = 2

        # TODO: enable setting this
        self.max_search_dist = 4
        self.show_graph_calls = False

        # used by IndexDims and ArgShapes to compute index dimensions 
        self.target_nelem = 1e6

        # params is used to retrieve values during testing
        self.arg_order = None
        self.arg_gen_nodes = {} # arg_name => GenNode
        self.args_gnode = None

        # Graphs
        self.pred_graph = {}
        self.gen_graph = {} # idx => node, for generating inventory
        self.inf_graph = {}
        self.dims_graph = {}

        # These will be set to ge.ObservedValue nodes
        self.obs_dtypes = None
        self.obs_shapes = None
        self.obs_args = None
        self.dtypes_gfilt = None 
        self.dtypes = None 
        self.predicate_nodes = None
        self.data_format_gobj = None
        self.data_format_inode = None

        # Objects shared between graphs
        self.data_formats = None
        
        self.data_tensors = []
        self.shape_args = []
        self.dtype_rules = base.DTypeRules()
        self.index_preds = []
        self.num_returns = 0
        self.return_nodes = []
        self.return_tensors = []

        # None: success.  pr.ErrorReport or list of Fix objects is failure
        self.op_error = None  # None means success.
        self.framework_exc_msg = None

        # call time values
        self.arguments = {}
        self.returns = [] 

    def _set_gen_error_quotas(self, dtype_quota):
        self.dtype_err_quota = dtype_quota

    def _pred_node(self, pred_class, name=None):
        name = fgraph.node_name(pred_class, name)
        return self.pred_graph.get(name, None)

    def _pred_nodes(self, *pred_classes):
        return tuple(self._pred_node(n) for n in pred_classes)

    def _gen_node(self, gen_class, name=None):
        name = fgraph.node_name(gen_class, name)
        return self.gen_graph.get(name, None)

    def _inf_node(self, inf_class, name=None):
        name = fgraph.node_name(inf_class, name)
        return self.inf_graph.get(name, None)

    def _init_schema(self, framework_mod, framework_op, init_schema_func):
        # edges to create for the pred graph
        self.framework_mod = framework_mod
        self.framework_op = framework_op
        self.pending_pred_edges = {} # node name -> [parent node name, ...]
        self.pending_index_edges = {} # node name -> [idx, idx, ...]
        self.func_sig = inspect.signature(framework_op)
        self.arg_order = list(self.func_sig.parameters.keys())
        self._init_pred_graph()
        self._init_inf_graph()
        self._init_gen_graph()
        self._init_dims_graph()
        init_schema_func(self)
        self._finalize()

        def wrapped_op(*args, **kwargs):
            # executes during 'framework call phase'
            try:
                self.op_error = self._check_args(*args, **kwargs)
            except BaseException as ex:
                raise OpGrindInternalError(ex)
            try:
                ret_val = self.framework_op(**self.arguments)
            except BaseException as ex:
                exc_str = str(ex)
                mt = re.match('\{\{.+?\}\} (.+)', exc_str)
                if mt is None:
                    self.framework_exc_msg = exc_str 
                else:
                    self.framework_exc_msg = mt.groups()[0]
                raise ex
            finally:
                self._check_return(ret_val)
                msg = self._report()
                if msg is not None:
                    print(msg, file=sys.stderr)
                return ret_val

        self.wrapped_op = wrapped_op
        return wrapped_op

    def _check_args(self, *args, **kwargs):
        """
        The main function to check all input arguments for all constraints
        registered on the schema.
        Return            Meaning
        None              success
        pr.ErrorReport    local error
        base.Fix list     more complex errors

        How many fixes do we want?
        
        """
        fixes = []
        bind = self.func_sig.bind(*args, **kwargs)
        bind.apply_defaults()
        self.arguments = bind.arguments
        self.returns.clear()
        self.framework_exc_msg = None
        self.inf_result = None

        for dist in range(self.max_search_dist+1):
            self.avail_edits = dist
            # returns the value of the first failing predicate node, or
            # none if all succeed
            ret = fgraph.pred_graph_evaluate(*self.predicate_nodes)
            if isinstance(ret, pr.ErrorReport):
                # error occurred in one of the single-argument handling nodes
                return ret
            elif ret == []:
                # no fixes found at this edit distance
                continue
            elif ret is None:
                # success
                # by definition, there was one fix from pr.Inventory, and
                # it has zero edit distance
                fix = self.inventory_node.get_cached()[0]
                self.inf_result = fix.shape 
                return None
            else:
                # ret is a list of Fix objects
                if not isinstance(ret, list):
                    raise RuntimeError(f'Got pred graph type {type(ret)}')
                return ret
                # fixes.extend(ret)
        # No fixes found
        return pr.ErrorReport(pr.NoSuggestionsFound())

    def _check_return(self, op_return):
        """
        Check the return tensors' shapes and types against those predicted by
        the framework
        """
        if self.op_error is not None:
            return

        if not isinstance(op_return, (list, tuple)):
            op_return = (op_return,)

        self.returns = list(op_return)
        error = fgraph.pred_graph_evaluate(*self.return_nodes)
        self.op_error = error

    def _shape_key_order(self, shape_keys):
        def key_fun(shape_key):
            pfx = shape_key.split('.')[0]
            if pfx in self.arg_order:
                return self.arg_order.index(pfx)
            else:
                m = re.match('return\[(\d+)\]', shape_key)
                ind = int(m.group(1))
                return len(self.arg_order) + ind

        key_order = sorted(shape_keys, key=key_fun)
        return key_order

    # TODO: fix headers (possibly integrate a header function in a NodeFunc base
    # class?)
    def _inventory(self):
        """
        Generate a usage inventory for the op.  Includes all combinations of
        input signatures, data format, dtypes
        """
        ranks = self._gen_node(ge.IndexRanks)
        sigs = self._gen_node(ge.SigMap)
        dtypes = self.dtypes_filt
        data_format = self._gen_node(ge.DataFormat)
        out_nodes = (ranks, sigs, dtypes, data_format)
        gen = fgraph.gen_graph_values(self.gen_live_nodes, out_nodes)

        inventory = list(gen)

        # includes args and returns.  args may have a '.k' suffix
        all_sigs = inventory[0][1]
        shape_args = [ *all_sigs ]
        if self.data_formats.configured:
            shape_args.append(self.data_formats.arg_name)
        arg_order = self._shape_key_order(shape_args)

        rows = [arg_order]
        shape_types = (ge.DataTensor, ge.ShapeList, ge.ShapeInt, ge.ShapeTensor)

        for ranks, sigs, dtypes, data_format in inventory:
            row = []
            for arg in arg_order:
                node = self.arg_gen_nodes.get(arg, None)
                func = None if node is None else node.func
                if isinstance(func, ge.DataFormat): 
                    row.append(cand_format)
                elif isinstance(func, shape_types):
                    sig = sigs[arg]
                    inst = ''.join(s * ranks[s] for s in sig)
                    row.append(inst)
                if isinstance(func, ge.DataTensor):
                    dtype = dtypes[arg].name
                    row.append(dtype)
                else:
                    pass
            rows.append(row)

        table, _ = tabulate(rows, '  ', left_align=True)
        return table

    def _index_inventory(self):
        """
        Generate a formatted report of the indices with their rank constraints
        """
        rows = []
        rows.append(['Index', 'Description'])
        rows.extend([ix,desc] for ix,desc in self.index.items())
        tab, _ = tabulate(rows, '  ', left_align=True)
        return tab

    def _report(self):
        if self.op_error is None:
            msg = None
        elif isinstance(self.op_error, list):
            fixes = self.op_error
            obs_dtypes = self.obs_dtypes.get_cached()
            obs_shapes = self.obs_shapes.get_cached()
            obs_args = self.obs_args.get_cached()
            rep = report.Report(self, fixes, obs_dtypes, obs_shapes, obs_args)
            msg = rep.report()
        elif isinstance(self.op_error, pr.ErrorReport):
            msg = self.op_error.report()
        else:
            raise RuntimeError(
                f'Unknown type of input error: {type(self.op_error)}')
        return msg

    def _report_edit_summary(self):
        """
        In case of TP and FP, provides a single-line summary of the list of
        suggested edits.  For TN, the empty string.  For FN, the framework
        exception in string form.
        """
        if self.op_error is None:
            msg = '' 

        elif isinstance(self.op_error, list):
            fixes = self.op_error
            items = [ f.summary() for f in fixes]
            msg = ', '.join(items)

        elif isinstance(self.op_error, pr.ErrorReport):
            msg = self.op_error.report()
        else:
            raise RuntimeError(
                f'Unknown type of input error: {type(self.op_error)}')

        msg += '\t' + (self.framework_exc_msg or '')

        # summarize returns if any
        items = []
        for ret in self.returns:
            item = f'{ret.shape.as_list()}:{ret.dtype.name}'
            items.append(item)
        if len(items) == 0:
            ret_msg = None
        else:
            ret_items = ', '.join(items)
            ret_msg = f'Return({ret_items})'
        finals = list(filter(None, (msg, ret_msg)))
        return '\t'.join(finals)

    def _get_arg(self, arg_name):
        """Retrieve the value of {arg_name} argument at call-time."""
        if arg_name not in self.arg_order:
            raise SchemaError(
                f'\'{arg_name}\' not a known parameter. '
                f'Known parameters are: {self.arg_order}')
        return self.arguments[arg_name]

    def _arg_shape_name(self, arg_name):
        if arg_name in [*self.data_tensors, *self.return_tensors]:
            return f'{arg_name}.shape'
        else:
            return arg_name

    def _check_sig(self, signature, name):
        if any(s not in self.index.keys() for s in signature):
            raise SchemaError(
                f'Signature "{signature}" associated with \'{name}\' '
                f'contains one or more unregistered indices. '
                f'Current known indices are: '
                f"{','.join(self.index.keys())}"
                f'Call OpSchema.add_index with the missing index.')

    def _init_pred_graph(self):
        P.set_registry(self.pred_graph)
        schema = P.add_node(pr.Schema(self))
        shapes = P.add_node(pr.ShapeMap())
        dtypes = P.add_node(pr.DTypes())
        argmap = P.add_node(pr.ArgMap())
        inventory = P.add_node(pr.Inventory(self), dtypes, shapes, argmap)
        self.inventory_node = inventory

        rten_pobj = pr.GetReturnTensors()
        rvalid_pobj = pr.ValidReturnShapes()
        rten = P.add_node(rten_pobj, schema)
        rvalid = P.add_node(rvalid_pobj, schema, rten)
        self.return_nodes = [schema, rten, rvalid]

    def _init_inf_graph(self):
        G.set_registry(self.inf_graph)
        self.obs_dtypes = G.add_node(nf.ObservedValue('dtypes'))
        self.obs_shapes = G.add_node(nf.ObservedValue('shapes'))
        self.obs_args = G.add_node(nf.ObservedValue('args'))
        layout_iobj = nf.Layout(self)
        layout = G.add_node(layout_iobj)
        index_ranks = G.add_node(nf.IndexRanks())
        dtypes_obj = nf.DTypes(self)
        self.dtypes = G.add_node(dtypes_obj, self.obs_dtypes, index_ranks,
                layout)
        sigs = G.add_node(ge.SigMap())

        indels_obj = nf.ArgIndels(self)
        arg_indels = G.add_node(indels_obj, index_ranks, sigs, self.obs_shapes,
                layout)

        usage_obj = nf.IndexUsage(self)
        idx_usage_inode = G.add_node(usage_obj, index_ranks, arg_indels,
                self.obs_shapes) 

        cons_obj = nf.IndexConstraints(self)
        cons_inode = G.add_node(cons_obj, idx_usage_inode, self.obs_args)

        report_obj = nf.Report(self)
        self.report_inode = G.add_node(report_obj, self.dtypes, cons_inode)

    def _init_dims_graph(self):
        G.set_registry(self.dims_graph)
        self.dims_input_node = G.add_node(ge.DimsInput())

    def _init_gen_graph(self):
        G.set_registry(self.gen_graph)
        layout_gobj = ge.Layout(self)
        layout = G.add_node(layout_gobj)
        index_ranks = G.add_node(ge.IndexRanks())
        impl_obj = ge.DTypesNotImpl(self)
        self.dtypes_gfilt = G.add_node(impl_obj, index_ranks, layout)
        sigs = G.add_node(ge.SigMap())
        arg_ranks = G.add_node(ge.ArgRanks(self), index_ranks, sigs)
        arg_indels = G.add_node(ge.ArgIndels(self), arg_ranks)
        arg_muts_obj = ge.ArgMutations(self)
        arg_muts = G.add_node(arg_muts_obj, arg_indels, index_ranks, sigs)
        self.args_gnode = G.add_node(ge.Args())

    def _finalize(self):
        # check that every index appearing in an 
        if self.data_formats is None:
            self.arg_layout(None, None, None)

        pred = set(self.pred_graph.values()).difference(self.return_nodes)
        self.predicate_nodes = pred

    def _prep_inference(self, obs_dtypes, obs_shapes, obs_args):
        self.obs_dtypes.set_cached(obs_dtypes)
        self.obs_shapes.set_cached(obs_shapes)
        self.obs_args.set_cached(obs_args)

    def _prep_gen_inventory(self):
        self.avail_test_edits = 0

    def _prep_gen_tests(self):
        self.avail_test_edits = 1

    def _generate_args(self):
        self._prep_gen_tests()

        live_kinds = (
                ge.DTypeEquiv, ge.DTypeIndiv, ge.DTypesNotImpl,
                ge.IndexRanks, ge.RankRange, ge.RankEquiv,
                ge.Layout, ge.Sig, ge.SigMap,
                ge.ArgRanks,
                ge.ArgIndels,
                ge.Options,
                ge.ArgMutations,
                )
        out_kinds = (
                ge.DTypesNotImpl,
                # ge.IndexRanks,
                # ge.SigMap,
                # ge.ArgRanks,
                ge.Options,
                ge.ArgIndels,
                ge.ArgMutations,
                )
        nodes = list(self.gen_graph.values())
        live = [n for n in nodes if isinstance(n.func, live_kinds)]
        out = [n for n in nodes if isinstance(n.func, out_kinds)] 
        live = nodes
        out = [self._gen_node(ge.Args)]

        ctr = 1
        for op_args in fgraph.gen_graph_values(live, out):
            # print(f'\r{ctr}', end='', flush=True)
            # ctr += 1
            # print(op_args)
            yield op_args[0] # extract tuple element

    def _validate(self, out_dir, test_ids):
        if not os.path.exists(out_dir):
            raise RuntimeError(
                f'{type(self).__qualname__}: Could not open output path '
                f'\'{out_dir}\' for report generation')

        report_fh = open(os.path.join(out_dir, f'{self.op_path}.txt'), 'w')
        summary_fh = open(os.path.join(out_dir, f'{self.op_path}.sum.txt'), 'w')
        cats = [ 'TP', 'TN', 'FP', 'FN' ]
        stats = { k: 0 for k in cats }

        op_args_gen = self._generate_args()
        # print(f'Generated {len(op_args_list)} test cases')

        for test_num, op_args in enumerate(op_args_gen, 1):
            if test_ids is not None:
                if len(test_ids) == 0:
                    break
                if test_num not in test_ids:
                    continue
                else:
                    test_ids.remove(test_num)

            # print(op_args)
            # self.show_graph_calls = True
            arg_dict = { k: v.value() for k, v in op_args.items() }
            string_err = io.BytesIO()
            try:
                with stderr_redirector(string_err):
                    self.wrapped_op(**arg_dict)
            except (SchemaError, OpGrindInternalError) as ex:
                print(string_err.getvalue().decode('UTF-8'))
                raise ex
            except BaseException as ex:
                pass

            if self.op_error is None:
                cat = 'TN' if self.framework_exc_msg is None else 'FN'

            elif isinstance(self.op_error, pr.ErrorReport):
                cat = 'FP' if self.framework_exc_msg is None else 'TP'

            else:
                assert isinstance(self.op_error, list)
                cat = 'FP' if self.framework_exc_msg is None else 'TP'

            stats[cat] += 1
            progress = '  '.join(f'{c}: {stats[c]:-5d}' for c in cats)
            print(f'\rTest: {test_num:-5d}  {progress}', end='')
            arg_fields = '\t'.join(f'{k}:{v}' for k,v in op_args.items())
            call = f'{test_num}\t{cat}\t{arg_fields}'
            print('\n\n', call, file=report_fh)
            print(f'Framework Msg\n{self.framework_exc_msg}\n', file=report_fh)
            print(self._report(), file=report_fh)
            edit_summary = self._report_edit_summary()
            summary = f'{call}\t{edit_summary}'
            print(summary, file=summary_fh)

        print()
        report_fh.close()
        summary_fh.close()

    # ============ PUBLIC API ====================
    def add_index(self, idx, description, rank_cons=None):
        """
        Add index {idx} with {description} to the schema.  {idx} must be a
        single letter and can be referred to in later signatures.

        {rank_cons} may be one of:

        1. an integer pair tuple of (<min_rank>, <max_rank>) 
        2. a single integer - interpreted as (<rank>, <rank>)
        3. the name of a previously registered index to equate the rank
        4. None - a constraint will be placed downstream limiting these ranks
        """
        if idx in self.index:
            raise SchemaError(f'Index \'{idx}\' already registered') 

        ranks_gnode = self._gen_node(ge.IndexRanks)
        ranks_inode = self._inf_node(ge.IndexRanks) # same class

        if isinstance(rank_cons, str):
            primary_idx = rank_cons
            if primary_idx not in self.index:
                raise SchemaError(f'Source index \'{primary_idx}\' is not '
                        f'a registered index')
            pri_ind = self.index[primary_idx]
            if pri_ind.fixed_rank():
                raise SchemaError(
                    f'Error declaring index \'{idx}\'. rank_cons = '
                    f'\'{rank_cons}\' identifies a fixed-rank '
                    f'index.  Only variable-rank indices can be used as '
                    f'rank equality constraints.  Use rank_cons=1 instead')
            elif pri_ind.pri_idx != primary_idx:
                raise SchemaError(f'Source index \'{primary_idx}\' is not '
                        f'a primary index')
            else:
                index = Index(idx, description, primary_idx, None)
                self.index[idx] = index

                G.set_registry(self.gen_graph)
                gobj = ge.RankEquiv(self, idx)
                pa = self.gen_graph[primary_idx]
                idx_gnode = G.add_node_sn(gobj, pa) 
                ranks_gnode.append_parent_sn(idx_gnode)

                G.set_registry(self.inf_graph)
                iobj = nf.RankEquiv(idx)
                ipa = self.inf_graph[primary_idx]
                idx_inode = G.add_node_sn(iobj, ipa) 
                ranks_inode.append_parent_sn(idx_inode)

        elif isinstance(rank_cons, (tuple, int, type(None))):
            if isinstance(rank_cons, int):
                rank_range = (rank_cons, rank_cons)
            else:
                rank_range = rank_cons

            G.set_registry(self.gen_graph)
            index = Index(idx, description, idx, rank_range)
            self.index[idx] = index

            idx_gobj = ge.RankRange(self, idx)
            idx_gnode = G.add_node_sn(idx_gobj)
            ranks_gnode.append_parent_sn(idx_gnode)

            G.set_registry(self.inf_graph)
            sigs_inode = self._inf_node(ge.SigMap)
            idx_iobj = nf.RankRange(self, idx)
            idx_inode = G.add_node_sn(idx_iobj, self.obs_shapes, self.obs_args)
            ranks_inode.append_parent_sn(idx_inode)

            if isinstance(rank_range, tuple):
                if not (len(rank_range) == 2 
                        and isinstance(rank_range[0], int) 
                        and isinstance(rank_range[1], int) 
                        and 0 <= rank_range[0] 
                        and rank_range[0] <= rank_range[1]):
                    raise SchemaError(
                        f'{type(self).__qualname__}: Got constraint tuple '
                        f'\'{rank_range}\' but it is not a 2-integer tuple')
                cons = base.SumRangeConstraint(idx, *rank_range)
                idx_gobj.add_schema_constraint(cons)
                idx_iobj.add_schema_constraint(cons)
            else:
                pass

            pri_inds = [ind.idx for ind in self.index.values() if ind.primary()]
            for pidx in pri_inds:
                if pidx == idx:
                    continue
                gpa = self.gen_graph[pidx]
                idx_gnode.append_parent_sn(gpa)
                ipa = self.inf_graph[pidx]
                idx_inode.append_parent_sn(ipa)

        else:
            raise SchemaError(
                f'{type(self).__qualname__}: Got constraint \'{rank_cons}\''
                f' but expected either an index, None, or an integer pair.')

    def arg_unchecked(self, arg_name):
        """
        Declare {arg_name} to be an argument unchecked by OpGrind 
        """
        pass

    def _get_dims_nodes(self, sig):
        pa_names = set()
        graph = self.dims_graph
        for idx in sig:
            if idx not in self.index:
                raise SchemaError(
                    f'Index \'{idx}\', found in sig \'{sig}\' is not '
                    f'yet registered with add_index')

            pa_name = next((name for name, nd in graph.items() if nd !=
                self.dims_input_node and idx in name), None)
            if pa_name is None:
                raise SchemaError(
                    f'Index \'{idx}\' mentioned in sig \'{sig}\' has not '
                    f'yet been registered by a call to comp_dims, gen_dims or '
                    f'gen_dims_func.  Must be registered before it is used '
                    f'as input to another call')
            pa_names.add(pa_name)
        nodes = [ self.dims_graph[n] for n in pa_names ]
        return nodes

    def _get_indexes(self, sig):
        inds = []
        for idx in sig: 
            ind = self.index.get(idx, None)
            if ind is None:
                raise SchemaError(
                    f'Index \'{ind}\' found in signature \'{sig}\' is not a '
                    f'registered index.  Register it first with add_index')
            inds.append(ind)
        return inds

    def _get_bcast_idx(self, sig):
        """
        Gets a single representative primary index of the indices in sig, if
        one exists among variable-rank indices.  If not, return None
        """
        inds = self._get_indexes(sig)
        vinds = [ ind for ind in inds if not ind.fixed_rank() ]
        if len(vinds) == 0:
            return None

        pri = { ind.pri_idx for ind in vinds }

        if len(pri) > 1:
            raise SchemaError(
                f'More than one primary index found in sig \'{sig}\': '
                ', '.join(pri)
                )
        pri_idx = pri.pop()
        return pri_idx

    def _get_rank_idx(self, in_sig, out_sig):
        pri_idx_in = self._get_bcast_idx(in_sig)
        pri_idx_out = self._get_bcast_idx(out_sig)
        if pri_idx_in is None or pri_idx_in == pri_idx_out:
            pri_idx = out_sig[0] if pri_idx_out is None else pri_idx_out
        else:
            raise SchemaError(
                f'One or more variable rank indices is present both in '
                f'in_sig \'{in_sig}\' and out_sig \'{out_sig}\', which do not '
                f'have the same primary index.  All indices in in_sig must '
                f'be fixed rank, or have the same primary index as those in '
                f'out_sig')
        return pri_idx

    def _gen_check_out_sig(self, out_sig):
        for idx in out_sig:
            if idx not in self.index:
                raise SchemaError(
                    f'idx \'{idx}\' is not yet registered with add_index')

        if out_sig in self.dims_graph:
            raise SchemaError(
                f'out_sig \'{out_sig}\' is already registered in '
                f'the dims_graph with one of the gen_dims_* or comp_dims '
                f'API functions')

    def gen_dims_func(self, out_sig, func, in_sig, max_prod, do_scalar, *pars):
        """
        Calls func(*in_dims, *pars), which yields one or more
        results.  Each result is either a tuple (lo, hi), if out_sig is a
        single index, or a tuple of such tuples, one for each index in out_sig 

        Multiplexes the call over the broadcasted collection of components in
        in_sig.  Broadcasting occurs if an index is integer or length-1 integer
        list.
        """
        try:
            self._gen_check_out_sig(out_sig)
        except SchemaError as ex:
            raise SchemaError(f'gen_dims_func got error: {ex}')

        pri_idx = self._get_rank_idx(in_sig, out_sig)
        gdims = ge.GenDims(out_sig, in_sig, func, pri_idx, do_scalar, max_prod,
                pars)
        G.set_registry(self.dims_graph)
        parents = self._get_dims_nodes(in_sig)
        G.add_node_sn(gdims, self.dims_input_node, *parents)

    def gen_dims(self, out_idx, max_prod):
        """
        Generate dimensions of {out_idx} of appropriate rank such that the
        product is in [1, max_prod]
        """
        def gen():
            yield 1, None
        try:
            self.gen_dims_func(out_idx, gen, '', max_prod, False)
        except SchemaError as ex:
            raise SchemaError(f'gen_dims got error {ex}')

    def gen_dims_rng(self, out_idx, min_val, max_val):
        """
        Generate dimensions of {out_idx} of appropriate rank such that the
        product is in [min_prod, max_prod]
        """
        def gen():
            yield min_val, max_val
        try:
            self.gen_dims_func(out_idx, gen, '', int(1e10), False)
        except SchemaError as ex:
            raise SchemaError(f'gen_dims_rng got error {ex}')

    def comp_dims(self, out_idx, func, tfunc, in_sig, *arg_names):
        """
        Register {func} as the formula defining {out_idx}, called as:
        func(*in_dims, *arg_vals), where in_dims are the run-time dimensions
        of indices in in_sig.

        indexes in in_sig must be broadcast-compatible with out_idx.  

        Option 1: in_sig indices and out_idx all have a constant rank of 1
        Option 2: every variable-rank index in in_sig has the same primary
        index as the primary index of out_idx.

        arg_names must be argument names registered with arg_option or
        arg_layout.
        """
        return self._comp_dims(out_idx, func, tfunc, in_sig, False, *arg_names)

    def comp_dims_cw(self, out_idx, func, tfunc, in_sig, *arg_names):
        """
        Same as comp_dims, except component-wise computation is performed.
        That is, the individual dimensions of each index dimension are fed to
        {func} one at a time to produce one output component.
        """
        return self._comp_dims(out_idx, func, tfunc, in_sig, True, *arg_names)

    def _comp_dims(self, out_idx, func, tfunc, in_sig, cwise, *arg_names):
        mut_gnode = self._gen_node(ge.ArgMutations)

        for arg_name in arg_names:
            if arg_name == base.LAYOUT:
                arg_gnode = self._gen_node(ge.Layout, base.LAYOUT) 
                mut_gnode.maybe_append_parent_sn(arg_gnode)

            elif arg_name in self.arg_order:
                arg_gnode = self.arg_gen_nodes[arg_name]
                mut_gnode.maybe_append_parent_sn(arg_gnode)

            else:
                raise SchemaError(
                    f'name \'{arg_name}\' in arg_names must be either a '
                    f'framework op argument, or the constant \'{base.LAYOUT}\''
                    f'Input arguments are: ' + '\, '.join(self.arg_order))

        if cwise:
            pri_idx = self._get_rank_idx(in_sig, out_idx)
        else:
            pri_idx = None
        cdims = ge.CompDims(self, out_idx, in_sig, cwise, func, tfunc, pri_idx,
                arg_names) 
        G.set_registry(self.dims_graph)
        parents = self._get_dims_nodes(in_sig)
        G.add_node_sn(cdims, self.dims_input_node, *parents)

        # add the non-negativity constraint
        self.dims_pred_rng(out_idx, 0, None) 

    def limit_ranks(self, sig, min_val, max_val):
        """
        Declare that the rank of {sig} be in [{min_val}, {max_val}]
        """
        self._check_sig(sig, 'rank limits')
        for idx in sig:
            if idx not in self.index:
                raise SchemaError(
                    f'Index \'{idx}\' mentioned in signature \'{sig}\' was '
                    f'not registered with add_index.  All indices must first '
                    f'be registered before being used in a limit_ranks call')

        # add constraint to each node in the sig
        pri_sig = ''.join(sorted(self.index[idx].pri_idx for idx in sig))
        cons = base.SumRangeConstraint(pri_sig, min_val, max_val)
        for idx in sig:
            pri_idx = self.index[idx].pri_idx
            gnode = self.gen_graph[pri_idx]
            gnode.func.add_schema_constraint(cons)
            inode = self.inf_graph[pri_idx]
            inode.func.add_schema_constraint(cons)

    def valid_dtypes(self, tensor_name, type_list):
        """
        Declare that {tensor_name} can have any of the dtype strings in
        {type_list}.  Names in {type_list} fit the pattern:

        ([a-z]+)(8|16|32|64|128)?([\+\-])?
        The first capture is the data type and must be one of:
        int, uint, float, qint, bfloat, bool, complex
        The second capture is the size.  It is optional.
        The third is an optional '+' or '-'.
        If the second is not present, the third must not be present.

        A prefix alone denotes all sizes of that data type are valid.
        A prefix with a quantity and no '+' or '-' specifies that single dtype.
        If a '+' is included, it means, that size and larger.
        If a '-' is included, it means that size and smaller.

        Can only be called once for a given {tensor_name}
        """
        if tensor_name not in self.data_tensors:
            raise SchemaError(
                f'{type(self).__qualname__}: Parameter \'{tensor_name}\' is '
                f'not registered as a tensor')

        dtype_names = [ t for ex in type_list for t in
                base.parse_dtype_expr(ex) ]
        # self.dtype_cons.add_valid(tensor_name, dtypes)

        # must be called before gobj creation
        self.dtype_rules.add_indiv_rule(tensor_name, dtype_names)

        G.set_registry(self.gen_graph)
        gobj = ge.DTypeIndiv(self, tensor_name)
        dtype_gnode = G.add_node(gobj)
        self.dtypes_gfilt.append_parent_sn(dtype_gnode)

    def equate_dtypes(self, trg_tensor, src_tensor):
        """
        Declare that {trg_tensor} have the same dtype as {src_tensor}.
        Both must be tensors declared with arg_tensor.
        Can only be called once for a given {trg_tensor}
        """
        if (src_tensor not in self.data_tensors or
            trg_tensor not in self.data_tensors):
            raise SchemaError(
                f'{type(self).__name__}: Can only be called on two tensors. '
                f'Parameters \'{src_tensor}\' and \'{trg_tensor}\' are not '
                f'both tensors.')

        # must be called before gobj creation
        self.dtype_rules.add_equate_rule(trg_tensor, src_tensor)

        G.set_registry(self.gen_graph)
        gobj = ge.DTypeEquiv(self, trg_tensor)
        src_dtype = self._gen_node(ge.DTypeIndiv, src_tensor)
        trg_dtype = G.add_node(gobj, src_dtype)
        self.dtypes_gfilt.append_parent_sn(trg_dtype)

    def exclude_combos(self, *field_value_pairs):
        """
        Allows to mark combinations of dtypes, ranks, and layout as excluded
        from the valid set.  This is useful for those cases that are not
        implemented by the framework.

        {field_val_pairs} is an even-length list of field, val, field, val, ...
        field is one of: 
        - data tensors registered in init_fields
        - one-letter index names registered in init_fields
        - the constant LAYOUT, if has_layout

        val is one of:
        - dtype string, such as 'int32' for data tensor fields
        - integer specifying a rank of an index field
        - the LAYOUT field has an integer in [0, num_layouts), as defined
          by the call to arg_layout.
        """
        if not self.dtype_rules.initialized:
            self.dtype_rules.init_fields(self.data_tensors, self.index.keys())
        try: 
            self.dtype_rules.add_combo(*field_value_pairs)
        except RuntimeError as ex:
            raise SchemaError(ex)

    def arg_int(self, arg_name, lo=None, hi=None):
        """
        Declare {arg_name} to be an integer that can take on values in a range.
        If {lo} is None, it is sys.maxint
        If {hi} is None, it is -sys.maxint-1 
        """
        G.set_registry(self.gen_graph)
        pred_obj = pr.ArgInt(arg_name, lo, hi)
        gen_obj = ge.Int(lo, hi)
        schema = self._pred_node(pr.Schema)
        p_arg = P.add_node(pred_obj, schema)
        g_arg = G.add_node(gen_obj)
        self.arg_gen_nodes[arg_name] = g_arg
        self.args_gnode.append_parent_sn(g_arg)

    def arg_option(self, arg_name, options):
        """
        Expect {arg_name} to take on one of the values in {options}
        """
        G.set_registry(self.gen_graph)
        options_gobj = ge.Options(self, arg_name, options)
        g_arg = G.add_node(options_gobj)
        self.arg_gen_nodes[arg_name] = g_arg
        self.args_gnode.append_parent_sn(g_arg)

        G.set_registry(self.inf_graph)
        options_iobj = nf.Options(self, arg_name, options)
        i_arg = G.add_node(options_iobj, self.obs_args)
        self.report_inode.append_parent_sn(i_arg)

        P.set_registry(self.pred_graph)
        options_pobj = pr.Options(arg_name, options_gobj, options)
        schema = self._pred_node(pr.Schema)
        p_arg = P.add_node(options_pobj, schema)
        arg_node = self._pred_node(pr.ArgMap)
        arg_node.append_parent_sn(p_arg)

    def arg_layout(self, arg_name, formats, rank_idx):
        """
        Declares {arg_name} to control layout-dependent signatures for tensors. 
        {layouts} is an array, where each element is a map of: rank => code
        The rank of {rank_idx} determines which layout is mapped.
        """
        if formats is not None:
            if not isinstance(formats, dict):
                raise SchemaError(f'arg_layout: formats must be a dict')
            if not all(isinstance(v, tuple) for v in formats.values()):
                raise SchemaError(f'arg_layout: formats must be a dict with '
                        f'tuple values')
        self.data_formats = base.DataFormats(arg_name, formats, rank_idx)
        
        # define the real arg 
        G.set_registry(self.gen_graph)
        layout = self._gen_node(ge.Layout, base.LAYOUT)
        ranks = self._gen_node(ge.IndexRanks)
        df_gobj = ge.DataFormat(self, self.data_formats, arg_name, rank_idx)
        df_gnode = G.add_node(df_gobj, ranks, layout) 
        self.data_format_gobj = df_gobj

        G.set_registry(self.inf_graph)
        layout_inode = self._inf_node(nf.Layout)
        ranks_inode = self._inf_node(nf.IndexRanks)
        df_iobj = nf.DataFormat(self, self.data_formats, arg_name)
        df_inode = G.add_node(df_iobj, ranks_inode, layout_inode, self.obs_args)
        self.data_format_inode = df_inode
        self.report_inode.append_parent(df_inode)

        if arg_name is not None:
            self.arg_gen_nodes[arg_name] = df_gnode
            self.args_gnode.append_parent_sn(df_gnode)

        P.set_registry(self.pred_graph)
        schema = self._pred_node(pr.Schema)
        data_format_obj = pr.DataFormat(self.data_formats, df_gobj, arg_name)
        p_arg = P.add_node(data_format_obj, schema) 

        arg_node = self._pred_node(pr.ArgMap)
        if arg_name is None:
            arg_node.append_parent(p_arg)
        else:
            arg_node.append_parent_sn(p_arg)

    def _check_sigs_layout(self, arg_name, sigs_list):
        if self.data_formats is None:
            # arg_layout is implicitly 1
            num_layouts = 1
        else:
            num_layouts = self.data_formats.num_layouts()
        if len(sigs_list) == 1:
            sigs_list = sigs_list * num_layouts

        if len(sigs_list) != num_layouts:
            raise SchemaError(
                f'{type(self).__qualname__}: registering \'{arg_name}\' '
                f'there are {self.num_layouts} '
                f'layouts (as established by the call to \'arg_layout\') but '
                f'{len(sigs_list)} elements of \'sigs\' argument.')
        return sigs_list 

    def _arg_shape_func(self, arg_name, sigs_list, shape_pnode, arg_gobj, kind): 
        """
        Backend function for arg_shape_* API functions.
        sigs_list must be a list of either 1 or num_layout elements.  If 1, it
        is implicitly broadcasted to num_layouts
        """
        sigs_list = self._check_sigs_layout(arg_name, sigs_list)
        P.set_registry(self.pred_graph)

        arg_gshapes = self._gen_node(ge.ArgMutations)
        arg_ishapes = self._inf_node(nf.IndexUsage)
        dtypes_gnode = self._gen_node(ge.DTypesNotImpl)

        G.set_registry(self.gen_graph)
        if isinstance(arg_gobj, ge.DataTensor):
            arg_gnode = G.add_node(arg_gobj, arg_gshapes, dtypes_gnode)
        else:
            arg_gnode = G.add_node(arg_gobj, arg_gshapes)

        G.set_registry(self.inf_graph)

        self.args_gnode.append_parent_sn(arg_gnode)
        # self.args_inode.append_parent_sn(arg_inode)

        self.arg_gen_nodes[arg_name] = arg_gnode
        
        G.set_registry(self.gen_graph)
        sigmap_gnode = self._gen_node(ge.SigMap)
        layout_gnode = self._gen_node(ge.Layout, base.LAYOUT)
        sig_gobj = ge.Sig(self, arg_name, sigs_list)
        sig_gnode = G.add_node(sig_gobj, layout_gnode)
        sigmap_gnode.append_parent_sn(sig_gnode)

        G.set_registry(self.inf_graph)
        sigmap_inode = self._inf_node(ge.SigMap)
        layout_inode = self._inf_node(ge.Layout)
        sig_iobj = ge.Sig(self, arg_name, sigs_list)
        sig_inode = G.add_node(sig_iobj, layout_inode)
        sigmap_inode.append_parent_sn(sig_inode)

        shape_map = self._pred_node(pr.ShapeMap)
        shape_map.append_parent_sn(shape_pnode)
        self.shape_args.append(arg_name)

    def arg_tensor(self, arg_name, *sigs):
        """
        Register {arg_name} as a data tensor.  

        sigs are all strings of signatures.  If len(sigs) == 1, then it
        specifies a static signature regardless of whether 'arg_layout' was
        called.  If len(sigs) > 1, then arg_layout is required to be called
        before this call.
        """
        schema = self._pred_node(pr.Schema)
        shp_pobj = pr.TensorShape(arg_name)
        arg_gobj = ge.DataTensor(self, arg_name)
        arg_pobj = pr.DataTensor(arg_name, arg_gobj)
        arg_p = P.add_node(arg_pobj, schema)
        shp_pobj = pr.TensorShape(arg_name)
        shp_p = P.add_node(shp_pobj, arg_p)
        kind = ShapeKind.DataTensor
        self._arg_shape_func(arg_name, sigs, shp_p, arg_gobj, kind)

        P.set_registry(self.pred_graph)
        dtypes = self._pred_node(pr.DTypes)
        tensor_dtype_obj = pr.TensorDType(arg_name)
        dtype = P.add_node(tensor_dtype_obj, arg_p)
        dtypes.append_parent_sn(dtype)
        self.data_tensors.append(arg_name)

    def _arg_shape_list_base(self, arg_name, broadcast_mode=False, *sigs):
        """
        See arg_shape_bcast_list and arg_shape_list
        """
        P.set_registry(self.pred_graph)
        schema = self._pred_node(pr.Schema)
        arg_gobj = ge.ShapeList(self, arg_name)
        arg_pobj = pr.ShapeList(arg_name, arg_gobj, broadcast_mode)
        arg_p = P.add_node(arg_pobj, schema) 
        kind = ShapeKind.List
        self._arg_shape_func(arg_name, sigs, arg_p, arg_gobj, kind)

    def arg_shape_bcast_list(self, arg_name, *sigs):
        """
        Register {arg_name} as an integer list parameter which defines the
        shape of a signature.  

        Expect arg_name value to be list of non-negative integers.
        If arg_val is length 1, interpret it as a generic broadcasted shape of
        unspecified rank.
        """
        self._arg_shape_list_base(arg_name, True, *sigs)

    def arg_shape_list(self, arg_name, *sigs):
        """
        Register {arg_name} as an integer list parameter which defines the
        shape of a signature.  

        Expect arg_name value to be list of non-negative integers defining a
        shape.  In contrast to arg_shape_bcast_list, here there is no
        broadcasting interpretation.
        """
        self._arg_shape_list_base(arg_name, False, *sigs)

    def arg_shape_int(self, arg_name, index, lo=None, hi=None):
        """
        Register {arg_name} as an integer parameter which defines the shape of
        an index.  The shape will be the broadcasted value of the argument if
        the index has rank greater than 1.

        If {lo} and/or {hi} are provided, the argument is additionally
        validated to be in that range.
        """
        # TODO: currently uses arg_shape_func, which assumes the arg defines
        # the rank.  In this case, it only defines the shape as the integer 
        # value broadcasted {rank} times.  But, the rank is not determined from
        # this input
        P.set_registry(self.pred_graph)
        schema = self._pred_node(pr.Schema)
        gen_obj = ge.ShapeInt(arg_name)
        ind = self.index[index]
        pred_obj = pr.ShapeInt(arg_name, lo, hi)
        arg_p = P.add_node(pred_obj, schema)
        kind = ShapeKind.Int
        self._arg_shape_func(arg_name, (index,), arg_p, gen_obj, kind)

    def arg_shape_tensor(self, arg_name, *sigs):
        """
        Register {arg_name} as a 1D integer tensor whose elements define the
        shape of a signature.  
        """
        P.set_registry(self.pred_graph)
        schema = self._pred_node(pr.Schema)
        gen_obj = ge.ShapeTensor(arg_name)
        pred_obj = pr.ShapeTensor(arg_name, gen_obj)
        arg_p = P.add_node(pred_obj, schema)
        kind = ShapeKind.Tensor
        self._arg_shape_func(arg_name, sigs, arg_p, gen_obj, kind)

    def arg_shape_tensor2d(self, arg_name, *sigs):
        """
        Register {arg_name} as a 2D integer tensor 'ten' defining the shape of
        sigs.  

        In the single layout case, sigs[i] are strings, and ten[d,i]
        defines dims(sigs[i])[d].  

        In the multiple layout case, sigs[i][l] is the i'th signature for
        layout l, and ten[d,i] defines dims(sigs[i][l])[d]

        Examples:

        Single layout case:

        ten = [ [1,2], [3,4], [5,6] ]
        sigs = ('b', 'e')
        
        defines 
        dims('b') := [1,3,5]
        dims('e') := [2,4,6]

        Multiple layout case:

        ten = [ [1,2], [3,4], [5,6] ]
        sigs = (('b', 'e'), ('e', 'b'))

        defines:
        layout 0: dims('b') = [1,3,5], dims('e') = [2,4,6] 
        layout 1: dims('b') = [2,4,6], dims('b') = [1,3,5]
        """
        # created nodes
        # pr.ShapeTensor2D, ge.ShapeTensor2D
        # ge.Sig(i)
        # pr.SliceShape(i)

        # created edges
        # pr.ShapeTensor2D -> pr.Schema
        # ge.ShapeTensor2D -> ge.GetArgShapes
        # pr.SliceShape(i) -> pr.ShapeTensor2D
        # ge.Sig(i) -> ge.Layout
        # ge.SigMap -> ge.Sig(i)
        P.set_registry(self.pred_graph)
        G.set_registry(self.gen_graph)
        schema = self._pred_node(pr.Schema)
        shape2d_gobj = ge.ShapeTensor2D(arg_name, len(sigs))
        shape2d_pobj = pr.ShapeTensor2D(arg_name, shape2d_gobj, len(sigs))
        p_shape2d = P.add_node(shape2d_pobj, schema)

        arg_shapes = self._gen_node(ge.ArgMutations)
        g_shape2d = G.add_node(shape2d_gobj, arg_shapes)
        self.arg_gen_nodes[arg_name] = g_shape2d
        self.args_gnode.append_parent_sn(g_shape2d)

        g_sig_map = self._gen_node(ge.SigMap)
        g_layout = self._gen_node(ge.Layout, base.LAYOUT)
        p_shape_map = self._pred_node(pr.ShapeMap)

        sigmap_inode = self._inf_node(ge.SigMap)
        layout_inode = self._inf_node(ge.Layout)

        for i, sig in enumerate(sigs):
            prefix = f'{arg_name}.{i}'
            # pr.ShapeMap -> pr.SliceShape
            shp_pobj = pr.SliceShape(arg_name, i)
            p_shp = P.add_node(shp_pobj, p_shape2d)
            p_shape_map.append_parent_sn(p_shp)

            if isinstance(sig, str):
                sig = [sig]
            G.set_registry(self.gen_graph)
            g_sig_obj = ge.Sig(self, prefix, sig)
            g_sig = G.add_node(g_sig_obj, g_layout)
            g_sig_map.append_parent_sn(g_sig)

            G.set_registry(self.inf_graph)
            sig_iobj = ge.Sig(self, prefix, sig)
            sig_inode = G.add_node(sig_iobj, layout_inode)
            sigmap_inode.append_parent_sn(sig_inode)

    def arg_rank(self, arg_name, sig):
        """
        Register {arg_name} to be an integer argument which defines the rank of
        {sig}
        """
        cons_name = f'rank({sig}) == \'{arg_name}\''

        P.set_registry(self.pred_graph)
        rank_pobj = pr.ArgInt(arg_name, 0, None)
        schema = self._pred_node(pr.Schema)
        rank_inode = P.add_node(rank_pobj, schema)

        P.set_registry(self.inf_graph)
        arg_node = self._pred_node(pr.ArgMap)
        arg_node.append_parent_sn(rank_inode)

        G.set_registry(self.gen_graph)
        g_ranks = self._gen_node(ge.IndexRanks)
        rank_gobj = ge.RankInt(arg_name, sig)
        rank_gnode = G.add_node(rank_gobj, g_ranks)
        self.arg_gen_nodes[arg_name] = rank_gnode
        self.args_gnode.append_parent_sn(rank_gnode)

        # TODO: add schema constraint, 
        cons = base.SigRankValueConstraint(arg_name, sig)
        for idx in sig:
            pri_idx = self.index[idx].pri_idx
            inode = self.inf_graph[pri_idx]
            inode.func.add_args_constraint(cons)

    def rank_dims_constraint(self, get_dims, rank_sig, shape_arg):
        """
        Creates a constraint called {constr_name} in the inference graph
        with the logic: RANK(rank_sig) == get_dims(shape_arg)
        """
        # add the constraint to the inference graph 
        cons = base.ShapeFuncConstraint(rank_sig, get_dims, shape_arg)
        for idx in rank_sig:
            pri_idx = self.index[idx].pri_idx
            inode = self.inf_graph[pri_idx]
            inode.func.add_shapes_constraint(cons)

    def dims_pred(self, pred_name, pfunc, pfunc_t, indices, *extra):
        """
        Registers {pfunc} with the schema to be used as an additional
        predicate for {indexes} dimensions.

        {pfunc_t} is a template function.  It is called with the index
        descriptions of each index in indices, and returns a string
        interpolating them, which explains the predicate logic.

        {pred_name} is a name given to this custom predicate.  It may be used
        in error messages.

        Called as pfunc(*index_shapes, *extra), where index_shapes
        are the resolved shapes of {indices}.  
        """
        if len(extra) > 0:
            pfunc = Partial(pfunc, *extra)
            pfunc_t = Partial(pfunc_t, *extra)

        pred = base.IndexPredicate(pred_name, False, pfunc, pfunc_t, indices)
        self.index_preds.append(pred)

    def dims_pred_cw(self, pred_name, pfunc, pfunc_t, indices, *extra):
        """
        Like dims_pred, but pfunc is called 'component-wise'.  That is, it is
        called once for each set of broadcasted index shapes.
        """
        if len(extra) > 0:
            pfunc = Partial(pfunc, *extra)
            pfunc_t = Partial(pfunc_t, *extra)

        pred = base.IndexPredicate(pred_name, True, pfunc, pfunc_t, indices)
        self.index_preds.append(pred)

    def dims_pred_rng(self, idx, lo, hi):
        """
        Register a predicate to test that idx dims are in [lo, hi].  If lo or
        hi are None, indicates no restriction.
        """
        if lo is None and hi is None:
            raise SchemaError(
                f'dims_pred_rng: error adding dims pred for {idx}.  '
                f'at least one of lo or hi must be an integer')

        leq = lambda v, m: v <= m
        leq_t = lambda v, m: f'{v} must be <= {m}'
        geq = lambda v, m: v >= m
        geq_t = lambda v, m: f'{v} must be >= {m}'
        betw = lambda v, lo, hi: lo <= v <= hi
        betw_t = lambda v, lo, hi: f'{v} must be in [{lo}, {hi}]'

        if lo is None:
            self.dims_pred_cw(f'{idx} >= {lo}', leq, leq_t, idx, hi)  
        elif hi is None:
            self.dims_pred_cw(f'{idx} >= {lo}', geq, geq_t, idx, lo)  
        else:
            name = f'{idx} in [{lo}, {hi}]'
            self.dims_pred_cw(name, betw, betw_t, idx, lo, hi) 

    # TODO: should I clone the graph, or simply set the parents to nodes in the
    # gen graph?
    def return_tensor(self, *sigs):
        """
        Append a return tensor to the list of expected return tensors.

        *sigs may contain either one element, or {num_layout} elements.  If one
        element, it defines the static signature for the return tensor.  If
        multiple, they are defined by the provided layout as declared in
        'arg_layout'
        """
        index = self.num_returns
        ret_name = f'return[{index}]'
        self.return_tensors.append(ret_name)
        sigs_list = self._check_sigs_layout(ret_name, sigs)

        P.set_registry(self.pred_graph)
        G.set_registry(self.gen_graph)

        g_sig_obj = ge.Sig(self, ret_name, sigs_list)

        G.set_registry(self.inf_graph)
        layout_inode = self._inf_node(ge.Layout)
        sig_iobj = ge.Sig(self, ret_name, sigs_list)
        sig_inode = G.add_node(sig_iobj, layout_inode)
        sigmap_inode = self._inf_node(ge.SigMap)
        sigmap_inode.append_parent_sn(sig_inode)

        self.num_returns += 1

