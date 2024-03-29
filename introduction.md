# Towards a robust TensorFlow frontend

TensorFlow ops are highly polymorphic, accepting sometimes thousands of combinations
of tensor ranks, dtypes, data formats, and other control settings.  In many cases,
the precise rules to determine if inputs are valid are not documented at the top
level.  Moreover, violating the hidden constraints often results in cryptic exception
messages arising very deep into the stack, or even program `abort()`.  In this
note, I present a proposed solution and proof-of-concept repo.

# Introducing a Tensor Op Specification API

This proof-of-concept repo defines an API for building *schemas* for TensorFlow ops,
described in the [README.md#Schema](README.md#schema).  Briefly, a schema concisely
defines what makes a valid set of inputs, in terms of tensor shapes, dtypes, and
other control parameters.  To do this, it introduces the notion of a semantic group
of indices (i.e. 'batch', 'input spatial') which are rank agnostic, and builds tensor
shapes as a sequence of these groups, called `signatures`.  Finally, groups of
signatures known as a `layout` correspond with choices of `data_format` arguments.
These intermediate constructs provide concise handles for defining validity
constraints and generating meaningful error messages and documentation using
consistent language. 

The API has evolved a lot as I have applied it to different ops.  For instance,
I hoped that `valid_dtypes` and `equate_dtypes` API calls would be sufficient to
describe what combinations of tensor dtypes ops would accept.  However, some ops
support almost all of a set of dtype combinations defined this way, but exclude a
small number, with a 'not implemented' error.  So, I added a third `exclude_combos`
API call to address this.

Similarly, I added the `arg_rank` API call specifically for `tf.gather_nd`
`batch_dims` argument - an argument which defines the number of a group of semantic
dimensions.  Almost no other ops use it, but it is necessary to properly describe the
behavior of `tf.gather_nd`.  Also the API call `rank_dims_constraint` was introduced
for `tf.gather_nd` to declare the constraint `rank(read_location) = indices.shape[-1]`.

`arg_shape_tensor` was introduced for `tf.nn.space_to_batch` `block_shape` argument,
and `arg_shape_tensor2d` for its `paddings` argument.

Ultimately, it would be ideal to have a common set of constructs, which can be used
for any op, and which are powerful enough to completely define the set of valid
inputs for that op.  To my knowledge, no such formal language exists, nor does any
complete description of valid inputs exist for many ops.  Note that the proposed
API does not attempt to describe *what* an op calculates - it only describes what
makes a set of arguments a valid input or not.

# Properties of a robust TensorFlow frontend

Here I propose what properties a frontend would need to have in order to be
considered robust.  Such a frontend would provide benefits to users and core
developers alike.  Users would receive much more actionable exceptions, and have a
clearer idea how to use TensorFlow's ops properly in the first place.  Core
developers would have a self-contained place to define the rules of the frontend for
each op.  As I'll show later, the proposed mechanism also provides comprehensive unit
testing examples, which already have revealed bugs in several ops.

* **Early checking** - inputs should be checked as early as possible, exceptions
  raised very low in the stack

* **No abort()** - It should be impossible to cause TensorFlow to abort from  incorrect
  inputs

* **Argument names in exceptions** - The exception message should use actual
  names of op arguments rather than e.g. `input #2(zero-based)` 

* **Specific exception messages** - exception messages should be specific but not
  make too strong assumptions about what fixes are needed

* **Accurate documentation** - documentation should describe the full range of
  allowed inputs. 

To achieve this, a precise mechanism for defining the set of valid inputs for an op
would be needed, which I propose here.  It turns out that this mechanism produces two
other things almost for free.  First, it provides a way to generate a thorough set of
valid and invalid test cases, useful for unit testing.  Second, a way to generate
human-readable documentation that precisely describes the rules that the valid inputs
must follow.

The mechanism consists of a compact API for building up the set of rules
incrementally.  A typical op requires about 50 lines of code written in this API to
fully define the op's *schema*.

# Current examples

Some examples of current TensorFlow behavior are given below, which could be
considered less than the ideal of a robust frontend. 

## No Early Checking

TensorFlow does not have any early checking mechanism.  It appears that most
exceptions arise from very deep in the code, but the traceback is filtered using
`filter_traceback(fn)` from
[tensorflow/python/util/traceback_utils.py](https://github.com/tensorflow/tensorflow/blob/master/tensorflow/python/util/traceback_utils.py) 

which states:

    Raw TensorFlow stack traces involve many internal frames, which can be
    challenging to read through, while not being actionable for end users.
    By default, TensorFlow filters internal frames in most exceptions that it
    raises, to keep stack traces short, readable, and focused on what's
    actionable for end users (their own code).

The problem with filtering traceback is that it becomes less useful for core
developers to diagnose a problem.  Filtering the traceback seems just a way of
covering up the symptom of a deeper problem.  Either exceptions should be caught and
re-thrown, or they should not be allowed to arise so deep in the codebase.

## Abort() due to zero-size dimensions

Two current examples of `abort()` being called are `tf.nn.space_to_depth` and
`tf.raw_ops.LSTMBlockCell`.  For `tf.nn.space_to_batch`, abort happens for certain
combinations of datatypes and layout when input spatial dimensions are `[0, 0]`:

    ID  ARGS                                                                     exitcode
    52  data_format=NHWC  input=[24, 0, 0, 8]:qint8  block_size=13               -6
    260 data_format=NCHW  input=[4, 14, 0, 0]:qint8  block_size=28               -6
    356 data_format=NCHW_VECT_C  input=[73, 4, 0, 0, 4]:float16  block_size=82   -6
    372 data_format=NCHW_VECT_C  input=[93, 14, 0, 0, 4]:float32  block_size=75i -6

For `tf.raw_ops.LSTMBlockCell`, it appears that, for both float16 and float32, if the
cell dimension is zero (the dimension of wci, wcf, wco, b, w.shape[1], h_prev.shape[1] or
cs_prev.shape[1]), then the op calls `abort()`.

## Unclear Exception text 

Due to the fact that exceptions arise so deep in the stack and are not caught and
rethrown, the code has no access to the argument names, and so there is no easy way
to provide them in the exception message.  This lack of argument names can be seen in
the examples below.  Others make incorrect assumptions, or refer to quantities that
are not documented and not part of the op interface, using internal names.

All of these problems are likely a consequence of having insufficient context during
the exception.

```
Dimensions [4,1) of input[shape=[43]] must match dimensions [1,1) of
updates[shape=[94]] [Op:ScatterNd]

cannot compute Conv2D as input #1(zero-based) was expected to be a int32 tensor but
is a uint64 tensor [Op:Conv2D]

Value for attr 'T' of int16 is not in the list of allowed values: half, bfloat16,
float, double, int32

Biases must be 1D: [] [Op:BiasAdd]

Tried to squeeze dim index 2 for tensor with 1 dimensions. [Op:Squeeze]

Conv2DSlowBackpropInput: input and out_backprop must have the same batch size.  Input
batch: 2, outbackprop batch: 97, batch_dim: 0 [Op:Conv2DBackpropInput]

No algorithm worked!  Error messages: [Op:Conv2DBackpropInput]

Conv2DSlowBackpropInput: filter and out_backprop must have the same out_depth
[Op:Conv2DBackpropInput]

input_sizes must be 4-dimensional, got: 3 [Op:Conv2DBackpropInput]

integer division or modulo by zero
```


## Inaccurate Documentation

Many ops lack robust documentation.  For instance,
[tf.nn.convolution](https://www.tensorflow.org/api_docs/python/tf/nn/convolution)
states (paraphrasing):

    `input` has a shape of:
        [batch_size] + input_spatial_shape + [in_channels]
        [batch_size] + [in_channels] + input_spatial_shape

    `filters` has a shape of `spatial_filter_shape + [in_channels, out_channels]`

It is not mentioned anywhere, but `filters` first channel merely needs to divide
evenly into `in_channels`.

Secondly, the op actually accepts numbers of batch dimensions varying at least
between 1 and 5, possibly more.  The documentation seems to imply it only accepts 1
batch dimension.

Finally, there is no documentation on what dtypes are accepted.  By experimentation,
it seems that the op accepts `int32`, all sizes of `float`, and `bfloat16`.  But, for
`int32` and `bfloat16`, there are certain exceptions in which ranks or data_formats
are implemented.  These exclusions may also be device dependent.

# Current Approach

Taking `tf.nn.convolution` as an example, it is in
[nn_ops.py](https://github.com/tensorflow/tensorflow/blob/master/tensorflow/python/ops/nn_ops.py#L1264#L1300)
where both argument integrity checks and routing to specialized implementations
begins:

```python
# /tensorflow/python/ops/nn_ops.py:1269
  if num_spatial_dims not in {1, 2, 3}:
    raise ValueError(
        "`num_spatial_dims` must be 1, 2, or 3. "
        f"Received: num_spatial_dims={num_spatial_dims}.")
  ...
  if name:
    default_name = None
  elif not has_tpu_context or call_from_convolution:
    default_name = "convolution"
  elif num_spatial_dims == 2:  # Most common case.
    default_name = "Conv2D"
  elif num_spatial_dims == 3:
    default_name = "Conv3D"
  else:
    default_name = "conv1d"
```

Further checks are performed in the rank-specific and device specific
implementations:

```cpp
// conv_grad_filter_ops.cc:193 
class Conv2DBackpropFilterOp : public OpKernel ...

// conv_graph_filter_ops.cc:313 
class Conv2DCustomBackpropFilterOp : public OpKernel ...

// conv_grahp_ops_3d.cc:179 
class Conv3DBackpropInputOp : public OpKernel ...

// 4 conv_graph_ops_3d.cc:302 
class Conv3DCustomBackpropInputOp : public OpKernel ...

// 5 conv_graph_ops_3d.cc:704 
class Conv3DBackpropFilterOp : public OpKernel ...
```

They all have sections for checking certain validity constraints such as:
- valid data_format
- stride == 1 for batch and input channel dimensions
- dilation == 1 for batch and input channel dimensions
- stride > 0
- proper dilation rank
- dilation > 0
- padding argument is one of allowed option
- filter is valid rank
- strides is valid rank
- input is valid rank
- input and filter have proper combination of ranks

These constraints are checked using calls to `OP_REQUIRES` or `CHECK` macros.  Many
of these constraints are not specific to the particular class and are therefore
redundant.  Besides duplicating code, this also raises the possibility of
inconsistent coverage of error messages, since each `OP_REQUIRES` call requires a
particular exception with its text.  At first glance it seems it would be much better
to have these checks at an earlier level, before these were called.

The approach opschema uses involves a construct `opschema.schema.Index`, a semantic
group of dimensions declared with `add_index` and associated with the shape of
tensors, as in:

```python
    op.add_index('f', 'filter spatial', 'i')
    op.add_index('j', 'filter input channel', 1)
    op.add_index('l', 'output channel', 1)

    op.arg_tensor('filters', 'fjl')
    ...
```

As will be shown, the opschema approach associates constraints on `Index` dimensions
and relationships.  It is only through the `Index` associations to the shapes of
tensors that tensor shapes are checked.  The checking can be done in a rank-agnostic
way.

# Overall Design Pitfall

Since user-facing TensorFlow ops are implemented as intricate compositions of lower
level ops, it is reasonable to hope that one can define top-level op constraints in
terms of locally defined lower level constraints.  Doing so would be very convenient
and maintainable.  When the implementation of an op changes, no extra work would be
needed for the exception handling to provide proper exceptions. 

Unfortunatley, in order to catch, translate a message, and rethrow exceptions, one
must have sufficient context at the point of the exception to construct a meaningful
message.  But, contextual information is lost on the way down and sometimes cannot be
recovered.  If the proper checks don't occur on the way down, it is too late in many
cases.

Therefore, the approach advocated in this work is to detect violations of
constraints as early as possible.  The function raising an exception must have
access to all of the top-level argument names and values.

In this work, I propose an API for defining these constraints and building the
checking function.  I have so far used it to implement schemas for these ops:

```bash
$ python -m opschema.cl list
tf.gather_nd
tf.nn.atrous_conv2d
tf.nn.atrous_conv2d_transpose
tf.nn.avg_pool
tf.nn.bias_add
tf.nn.conv_transpose
tf.nn.convolution
tf.nn.depth_to_space
tf.nn.separable_conv2d
tf.nn.space_to_batch
tf.nn.space_to_depth
tf.raw_ops.LSTMBlockCell
tf.scatter_nd
```

The schemas are not perfect descriptions of the TensorFlow op behavior.  In some
cases, this is due to a shortcoming of the schema.  In others, they reveal likely
bugs in the TensorFlow ops.

TODO: transition

# Comprehensive Unit tests

As will be described below, a schema which logically defines the set of valid inputs
for a given op contains all the information, in theory, needed to generate a full
range of valid inputs, as well as invalid inputs.  This can be extremely large due
to the combinatorics of polymorphism displayed by some of the ops.  For instance,
`tf.nn.convolution` supports at least five (so far tested) batch dimensions in
combination with 1, 2, or 3 spatial dimensions, producing 15 combinations based on
input tensor ranks.  Each of these can be one of two basic layouts: the 'channel
first' variety, with `data_format` NCW, NCHW or NCDHW, or the channel last' variety,
with `data_format` NWC, NHWC, or NDHWC.  padding can be either 'SAME' or 'VALID', so
that the combinations are now 5 * 3 * 2 * 2 = 60.

For each group of logical dimensions, we also want to test ordinary non-zero
dimension size, and the zero-size dimension edge case.  There are 8 different groups
bearing dimension sizes:  *batch*, *input spatial*, *input channel*, *filter
spatial*, *filter input channel*, *output channel*, *stride*, and *dilation*.  

In generating test cases, each of these could or could not contain a zero dimension,
giving 2^8 = 256 different combinations.  In addition, we would like to check edge
cases in which the input channel is or is not divisible by filter input channel.  Yet
another test is to choose input spatial and filter spatial dimensions that lead to
positive or negative output spatial dimensions, to test the behavior of exceptions.

On top of all of these combinations, there are allowed and disallowed dtypes for
'input' and 'filter' tensors.  We would like to test all valid combinations to
confirm proper behavior, as well as some selection of invalid combinations.

Using such comprehensive testing, I have discovered many configurations for
`tf.nn.space_to_batch` and `tf.raw_ops.LSTMBlockCell` which cause an `abort()`.  To
take another example, the convolution related ops `tf.nn.convolution`,
`tf.nn.conv_transpose` and `tf.nn.atrous_conv2d` raise an exception with the message
`No algorithm worked!` in many cases.

Below I outline the main principles of `opschema` and how it can be used to concisely
define schemas for TensorFlow ops.

# Principles of opschema

opschema provides a framework for defining *schemas* for TensorFlow ops which define
the set of all possible valid inputs.  In particular, this framework simultaneously
provides:

* predicate to judge input valid or not, with correct error messages
* generator to produce valid and invalid test inputs
* automated documentation describing the predicate logic

*opschema* internally constructs the generator and predicate functions based on the
*schema* API calls.  

Mathematically, the predicate and generator functions implicitly define the same set.
The predicate tests set membership.  The generator produces set members.  Here, the
*set* corresponds with an op, such as `tf.nn.convolution`.  Each element of the set
represents a valid setting of parameter values,  For example:

    # one set element
    input=[97, 13, 9]:float64
    filters=[3, 13, 20]:float64
    strides=[1]
    padding=SAME
    data_format=NCW
    dilations=5

As mentioned above, the number of valid + invalid edge case settings for an op is
exponential in the number of separate categories to be tested.

In order to achieve this combinatoric behavior in a flexible way, `opschema` builds
up a **predicate computation graph** and a **generator computation graph** to
implement the predicate and generator functions respectively.  Each API call from
`opschema.schema.OpSchema` creates nodes and/or edges in these graphs.  The graphs
themselves can be printed with:

    python -m opschema.cl graph OP_PATH OUT_DIR

which produces `OUT_DIR/OP_PATH.{gen,pred,inf,dims}.svg`

As a high level illustration, a predicate computation graph is a computation graph
whose nodes are special predicate functions.  The functions actually return a tuple
of `(success, value)`, where success is the predicate, and value is a value to be
input into child nodes' predicate functions.  The entire graph becomes a predicate
whose value is true if and only if all nodes evaluate to true.  To evaluate the
graph, the nodes are evaluated one by one in topological order, starting with nodes
that have no parents.

The generator computation graph is similar, except that the functions are generator
functions.  The entire graph becomes a generator function whose values reflect all
possible combinations of the individual generator nodes.  However, they aren't
independent - each generator function takes inputs from its parents, and is invoked
once for each possible setting of these inputs.

# opschema Index objects

The central construct of opschema is the `opschema.schema.Index` object.  Each Index
object represents a semantic group of indices, such as 'batch dimensions' or 'input
spatial dimensions'.  It is proposed as the single source for naming a group of
dimensions, for both documentation generation and Exception text generation.  The
Index provides a handle for defining other constraints on the rank (number of
dimensions) and relationships among different index dimension sizes, either
component-wise or not.

Indexes are useful to give names not only for dimensions appearing in the inputs or
outputs of an op, but intermediate values as well.  For instance, 

# `opschema` predicate graphs

opschema employs a predicate graph to check whether an op's argument set is valid or
not.  And, if not, to issue an informative error message.  Since it is a
proof-of-concept, it does not throw an exception, but rather passes the argument set
on to TensorFlow's op.  In this way, opschema's error message can be compared with
TensorFlow's exception should it arise.  Ultimately, if it were adopted, it would
instead be used to construct the exception text.

The predicate graph actually used by the `tf.nn.convolution` schema is shown here.

![Predicate Graph](graphs/tf.nn.convolution.pred.svg?raw=true)


All of the nodes except the `Inventory` node do something like 'enhanced
typechecking' of arguments.  Each node contains a function that both acts as a
predicate, and if successful, passes on a value to child nodes to be input into their
enclosed functions.  If unsuccessful, evaluation of the graph stops.  

The 'Schema' node takes no arguments and returns `(True, arg_dict)`.  Each child node
extracts a particular value based on its name.  For instance, `ShapeList(strides)`
takes `arg_dict['strides']` as input.  Its job is to validate that it is either a 
non-negative integer or list of non-negative integers.  If so, it returns `(True,
arg_dict['strides'])`.  If not, it returns `(False, ErrorReport)`, which is a class
that produces a human-readable error message.  The node `DataTensor(input)` extracts
`arg_dict['input']` and validates that it is a tensor, passing it along if so.

Nodes `TensorShape(input)` and `TensorDType(input)` always succeed - they are
guaranteed to receive a tensor, and their only job is to extract and pass on its
`shape` or `dtype` respectively.

At the next level, nodes `ShapeMap` and `DTypes` always succeed.  Their job is to
aggregate the outputs of their parents.  `ShapeMap` produces `{ 'input': input.shape,
'filters': filters.shape }` (the shapes of the input and filters tensors, respectively).

Similarly, the aggregator node `ArgMap` collects argument values of certain other
types.  In this case, it will be `{ 'padding': 'SAME', 'data_format': ... }`.
All of these aggregators feed into the `Inventory` node, where the main work
is done.

Note now that the input information, after being validated for the correct types, is
divided into shapes, dtypes, and everything else put in `ArgMap`.  The shapes consist
not just of tensor shapes but in this case other quantities (strides and dilations)
which participate in shape-based calculations.  The `ArgMap` are control parameters
that affect the interpretation and computation of the shapes.

Within the `Inventory` node, the predicate function is actually implemented by an
enclosed generative computation graph diagramed below:

![Inventory Graph](graphs/tf.nn.convolution.inf.svg?raw=true)

This is a generative graph nested inside a predicate node. Its job is to generate
possible interpretations of the arguments that are correct according to the schema
constraints, and agree with the observed arguments.  If it finds exactly one valid
interpretation that is consistent with observations, the predicate succeeds.

There are two sections of the graph which can generate independently.  Shown at the
top are groups of `RankEquiv` and `RankRange` nodes.  Each of these corresponds to
one opschema Index, and is responsible for generating each of the permitted ranks for
each Index.  For example, `RankRange(batch)` will generate 1, 2, 3, 4, 5.  This
expresses the fact that `tf.nn.convolution` supports at least 5 batch dimensions.
`RankRange(input_spatial)` will generate 1, 2, 3, specifying 1D, 2D, and 3D
convolutions.  Because of the combinatoric nature of the generative graph process,
this creates 15 possible combinations.  All of the other nodes either are constraint
to rank=1 (such as `input_channel` and `output_channel`) or are `RankEquiv` nodes
which simply yield the same rank as their parent.  Note that at this stage, the only
constraints on the rank combinations are those at the schema level.  There is no
checking for consistency with observed argument values at this point.

The `IndexRanks` node simply collects each rank into a single map of `index => rank`,
and yields it.

The bottom right section consists of the `Layout`, `Sig`, and `SigMap` nodes.
`Layout` is an unobserved state which in this case generates two codes: 0, and 1,
representing the notion of a 'channel first' or 'channel last' layout.  The channel
first layout corresponds with `data_format=NC*`.  The `Sig` nodes emit a *signature*
for a tensor or other shape-related argument such as 'strides'.  

In this case, for layout 0, `Sig(input)` yields `bki`, which is a string of the
`Index` one-letter codes, representing the sequence of indices `batch, input_channel,
input_spatial` In coordination with this, the `Sig(return[0])` node will emit `blo`,
representing `batch, output_channel, output_spatial`.  Together, this means that both
the `input` and `output` tensors will be channel-first.  Signatures are combined into
a map of `arg_name => signature` in the `SigMap` node.  Together with the
`IndexRanks` node, each signature can be instantiated by specific ranks.  For
example, given `rank(b) = 3, rank(k) = 1, rank(i) = 3`, the signature `bki` can be
instantiated as `bbbkiii`.  The instantiation provides a component-wise
interpretation of a shape that may be observed later on.

The `ObservedValue(shapes)` node yields a single item, a map of `arg_name =>
<shape>`, for each shape-bearing argument provided by the user.  In this case, this
will be:

    {
      'input': input.shape,
      'filter': filter.shape,
      'strides': strides,
      'dilations': dilations
    }

where `<shape>` is either a non-negative integer or list of non-negative integers. 

The `ArgIndels` node is the first piece of logic which combines the observed values
(shapes) with the hidden hypotheses (layout, signatures, and Index ranks).  If the
instantiated signature, (`bbbkiii` above) has a different rank than that of the
observed shape, `ArgIndels` creates an Insert or a Delete of the appropriate number
of dimensions.  If the ranks match, there is no edit and no associated cost.

There is a global setting for the maximum allowed cost before giving up, which is set
at the beginning of the search.  If that quota is exceeded, the `ArgIndels` node does
not yield for that particular input.  So, it acts as a context-dependent filter.  On
the other hand, if the budget has not been exhausted, `ArgIndels` will yield the
'edit' as a potential hypothesis.

`IndexUsage` detects whether each `Index` has consistent dimensions in every
signature instantiation in which it appears, by matching it up with the shape.  For
example:

    { 'b': 3, 'k': 1, 'i': 3, 'f': 3, 'l': 1 }                    # yielded by IndexRanks
    { 'input': 'bki', 'filter': fkl' }                            # yielded by SigMap
    { 'input': [10,5,2,9,100,100,100], 'filter': [10,10,10,9,5] } # yielded by ObservedValue(shapes)

then, `IndexUsage` would use the `IndexRanks` to instantiate the signatures, and
match them up with the shapes as follows:

    'input':  [ b1, b2, b3, k,  i1,  i2,  i3]
              [ 10,  5,  2, 9, 100, 100, 100]

    'filter': [ f1, f2, f3, k, l]  # Warning: this is not actually tf.nn.convolution!
              [ 10, 10, 10, 9, 5]

The only `Index` that occurs in more than one place is 'k'.  In this case, it has the
same dimension of 9.  But, if this dimension differed, then `IndexUsage` would again
either yield an 'edit' object while incrementing the current cost.  Or, if there was
no available budget, it would not yield this configuration, acting as a filter.

I put a warning showing though that in fact, the signature for `filter` doesn't use
'k', but a separate index called 'filter_input_channel'.

At the top of the graph in the next stage we have `DTypes`.  It also acts as a
filter.  If the combination of observed input tensor dtypes provided by
`ObservedValue(dtypes)` is allowed, `DTypes` will yield a map of `arg_name => dtype`.
If not, it will increment the errors and either yield or not depending on the budget.

`DTypes` also receives input from `IndexRanks` and `Layout`.  This is because there
are certain combinations of dtypes, ranks, and layouts which are not implemented, and
must be specially flagged.  In the case of `tf.nn.convolution`, these are:

    Excluded DType Combos

    input.dtype  rank(i)  layout
    int32        1,2      0       # int32 1D and 2D channel-first (layout 0)
    int32        3        *       # 3D int32 any layout
    bfloat16     1,2      *       # etc... 
    bfloat16     3        0 

This may also be device specific - these are the exclusions that seem to be in place
for a GTX-1070 GPU.  A refinement of the rules will likely be necessary.

The `IndexConstraints` node implements specially declared predicates on indices.  I
lied earlier about 'filters' having a signature of `fkl`.  In fact, it has signature
`fjl`, `j=filter_input_channel`.  This is necessary because `tf.nn.convolution`
allows these channels to have different dimensions, but there is a constraint:  'k'
must be divisible by 'j'.  This constraint is declared with the API function
`dims_pred_cw`, which accepts user-provided predicate function.  These predicate
functions are registered and called with the run-time dimensions of the indices as
they are instantiated by the `IndexUsage` node.  Then, `IndexConstraints` apply the
predicates and either increment the cost or do further filtering.

opschema will repeatedly try to run the overall predicate graph starting with a cost
budget of zero.  This will halt immediately if any error is encountered.  But, if it
yields something, it will be a unique interpretation of the inputs and signify that
the inputs are valid.

If it does not yield anything with a budget of zero, a budget of 1 is tried, and so
on.  These modes may produce more than one 'suggested fix', in which a fix is a
collection of possible edits that would restore correctness to the inputs.

The `DataFormat(data_format)` node receives the value of the `data_format` parameter
from `ObservedValue(args)`, as well as the `Layout` and possibly the rank of some
`Index` from `IndexRanks`.  In the case of `tf.nn.convolution`, the interpretation is
controlled by the schema content:

    formats = {
            'NCW': (0, 1),
            'NCHW': (0, 2),
            'NCDHW': (0, 3),
            'NWC': (1, 1),
            'NHWC': (1, 2),
            'NDHWC': (1, 3),
            None: (1, None),

    arg_layout('data_format', formats, 'i')

In my view it is unfortunate that `data_format` is defined in this way, since the
information designating `rank(i)` is redundant, and could instead be inferred from
the rank of the `filters` tensor.  When the two conflict, it is logically not clear
which one is intended to be authoritative.  opschema takes this into account and
considers both interpretations.

This also applies to the situation in which the shapes of `input` and `filters` might
be consistent with a 'channel first' data format, but the `data_format` argument is
one of the 'channel last' variety.  In that case, the user may very well have
intended a different data format.

The power of the generation graph with budget is that it can find hypotheses that are
the closest in edit distance to the given inputs.

Finally, the `Options(padding)` node validates that 'padding' is one of the valid
options 'VALID' or 'SAME', and incurs a cost if not.

The set of edits (if any) produced by the various nodes are aggregated in the
`Report` node and compiled into a `Fix` object, which summarizes this and provides
functionality for constructing error messages for the user.

# Generating Unit Tests

The next major functionality of opschema is the ability to generate a thorough set of
test inputs for an op, both valid and invalid.  This function relies on a generative
graph shown below:

![Generation Graph](graphs/tf.nn.convolution.gen.svg?raw=true)

It is similar to the generative portion of the predicate graph except for some key
differences.  First, the nodes do not keep track of any error budget, nor are there
any comparisons with observed values.  For instance, in
[the inference graph](graphs/tf.nn.convolution.inf.svg), the `Options(padding)`
node received input from the `ObservedValue(args)` node, checked it for validity, and
then yields the value if so, or an edit object if not.

In this graph, the `Options(padding)` node yields both of the valid values, as well as a
'DUMMY' value.  This is the case for all of the nodes - they yield the complete set
of valid values, plus some selected set of invalid values.  These invalid values
serve as inputs to cause TensorFlow to throw, and for opschema to issue an error
message.

In similar fashion to the inference graph, the generation graph first generates a
combination of Index ranks, and a set of signatures from a given Layout.  Combining
the signatures and Index ranks into ArgRanks.   At this stage, all values generated
are valid according to the schema constraints.  `ArgIndels` yields a map of `arg_name
=> indel` which in principle can contain multiple indels.  In this current
implementation, it contains either zero or one.  For each argument that has a shape
(and thus rank), `ArgIndels` yields the empty map `{}`, an `Insert` object at a
random position, and a `Delete` object at a random position.

The `ArgMutations` node yields a map of `arg_name => shape` for all shape-bearing
arguments.  In order to generate actual shapes, it first needs to generate dimensions
for each Index.  In the case of `tf.nn.convolution` there are 10 separate indices.
One of these, 'g' (dilated_filter_spatial) is an intermediate index, which does not
appear in any input or output tensor.  It is useful for more cleanly expressing the
final computed index 'o' (output_spatial).

The dimension generation process relies on a nested graph as follows:

![Index Dimensions Graph](graphs/tf.nn.convolution.dims.svg?raw=true)

This graph consists of two main types of nodes.  `GenDims(idx)` nodes generate dimensions
for `idx` according to certain limits specified by the schema.  This is where the
total number of tensor elements can be controlled.  Since many ops allow large
ranks, controlling the total size of tensors during the generation process is
crucial to avoid OOM error.  Here, for example, `tf.nn.convolution` uses:

```python
op.gen_dims(out_idx='b', min_val=1, max_val=100, max_prod=100, gen_zero=True)
```

This tells the opschema generation process to generate batch dimensions in [1, 100]
such that the product is also <= 100.  So, if it were generating 5 batch dimensions,
the total number of batch slices would be controlled.  The `gen_zero=True` flag tells
opschema to also generate additional test cases with batch dimensions of zero.
These are important test cases, either to confirm that TensorFlow op correctly
processes such inputs, or that it correctly throws an Exception.  In some cases,
these test cases cause `abort()`, depending on the combinations of indexes with zero
dimensions.

Some nodes must generate dimensions for multiple indices at a time, in order to
control certain relationships in their values.  `GenDims(input_channel,
filter_input_channel)` is one such example.  In this case, `tf.nn.convolution`
accepts cases where the components of `input_channel` are evenly divisible by
`filter_input_channel` (component-wise).  If opschema didn't generate these together,
there would be only very rare positive test cases occurring.  This node is registered
with a custom function specified by the schema writer to yield randomly generated
cases that follow this, as well as certain zero-dimension combinations and other
invalid combinations.  The complete function is shown here:

```python
def group_channels(rng, max_input_val):
    """
    Generate a pair of input, filter channel dims, in which input is divisible
    by filter
    """
    inp = rng.choice(range(1, max_input_val+1))
    factors = get_factors(inp)
    inp_tup = (inp, inp)
    filt = rng.choice(factors)
    filt_tup = filt, filt

    fake = next(filter(lambda i: inp % i != 0, range(1, inp+1)), inp)
    
    yield inp_tup, inp_tup
    yield inp_tup, filt_tup
    yield inp_tup, (0, 0)
    yield inp_tup, (fake, fake)
    yield (0, 0), filt_tup
    yield (0, 0), (0, 0)
```

The `CompDims(idx)` node produces computed dimensions.  In this case both
`dilated_filter_spatial` and `output_spatial` are computed.  As with custom-generated
dimensions, computed dimensions require user-provided functions (which can operate
component-wise or not).  Computed dimensions may need other non-dimension inputs,
which are provided by (in this case) the `DimsInput(padding)` node.

Declaring computed indexes and registering functions to compute them is at the core
of the functionality and clarity of opschema.  Here, the schema API calls defining
these computed dimensions is:

```python
# g = dilated_filter_spatial
# f = filter_spatial
# d = dilation
# i = input_spatial
# o = output_spatial
op.comp_dims_cw('g', dilate, dilate_t, 'fd') 
op.comp_dims_cw('o', strided_conv, strided_conv_t, 'igs', 'padding')

# from opschema/complib.py
def strided_conv(i, f, s, padding):
    if padding == 'VALID':
        return ceildiv(i - f + 1, s)
    else:
        return ceildiv(i, s)

# the 'template' function for producing human-readable trace of computed dimensions
def strided_conv_t(i, f, s, padding):
    if padding == 'VALID':
        return f'ceil(({i} + {f} - 1) / {s})'
    else:
        return f'ceil({i} / {s})' 
```

Every computed index comes with the main function and a 'template' function that
allows generating a human-readable trace of the computation.  These template
functions, together with intermediate Indexes, allow opschema to create a message
showing the user exactly how dimensions were computed.  In this example, the message
shows that the output dimensions were negative, and exactly how they were computed:

```bash
output_spatial (o) = [-8].  output_spatial must be >= 0

Dimensions computed as:
dilated_filter_spatial = filter_spatial + max(0, filter_spatial-1) * dilations
output_spatial = ceil((input_spatial + dilated_filter_spatial - 1) / strides)   [padding = VALID]

g = f + max(0, f-1) * d
o = ceil((i + g - 1) / s)   [padding = VALID]

[25] = [13] + max(0, [13]-1) * 1
[-8] = ceil(([16] + [25] - 1) / 1)   [padding = VALID]
```

This inner index dimension graph yields complete sets of dimensions for each Index,
as part of the operation of its containing graph.  The containing graph communicates
through the `DimsInput(padding)` and `DimsInput(_index_ranks)` nodes, which act as
stubs to allow the inner graph structure to be self-contained while still
participating in the combinatorial generation process.

Once a set of Index dimensions is generated from inside the `ArgMutations` node, it
uses these dimensions along with argument signatures to construct full argument
shapes by concatenation.  This is the inverse process of the `IndexUsage` inference
shown above.

    { 'b': [10, 5, 2], 'k': [9], 'i': [100, 100, 100] }   # from Dimensions graph
    { 'input': 'bki', 'filters': ... }                    # from Sig node

the `ArgMutations` node will construct:

    { 'input': [10, 5, 2, 9, 100, 100, 100], 'filters': ... } # yielded by `ArgMutations`

by concatenating the dimensions according to the signature.  It will also apply
Inserts and Deletes and yield these shapes separately.  In addition, it will also
create point 'mutations' of individual dimensions.  These mutations are important to
generate faulty test cases to test the error message generation and Exception
behavior.

Meanwhile, the dtype generation process involves `DTypeIndiv(input)`,
`DTypeEquate(filters)` and `DTypesNotImpl`, which mirror the inference process.
`DTypeIndiv(input)` generates valid (and a subset of invalid) dtypes for the 'input'
tensor according to the schema.  The valid types were declared as `int32`, `float*`
and `bfloat16`, all of which are yielded.  Then, the node will generate a budget of
up to `--max_dtype_err` additional invalid dtypes, randomly generated from a known
random seed.

The `DTypeEquate(filters)` node simply generates the matching dtype as it receives.
It too will generate a randomly chosen non-matching dtype as well, according to the
quota.

Finally, the `DTypesNotImpl` node applies a matching rule to the combination of
dtypes, as well as in this case the output of `IndexRanks` and `Layout` nodes.  If
the combination is flagged by the rule (i.e. known to be not implemented by
TensorFlow), then a current error count is tallied.  If this count exceeds a given
budget, it will not be yielded.

In this way, the total number of errors contained in any generated input can be
capped during the generation process.

Finally, the Args node constructs symbolic `oparg.OpArg` objects.  These can be of
different types.  The `oparg.DataTensorArg` stores the dtype and shape, and serves as
a stub for later generating an actual tensor.  A complete map of `arg_name => OpArg`
is yielded from the `Args` node, and the whole output is accessible as a generator
using:

    op.generate_args(rand_seed)


# TODO: Extra notes under construction

And suppose that it accepts even, non-negative integers.  If given an odd or negative
integer, it raises some exception, but the exception doesn't actually inform the user
what is wrong.  Also, suppose the function doesn't document that it only accepts
even, non-negative integers.  Our goal is to separately define a predicate function
that mimics the set of successful inputs of the op.  
A predicate for `func(i)` would be:

```python
def pred_even_ints(i):
  return i >= 0 and i % 2 == 0
```

Given this predicate, a generic recipe for a generator is:

```python
def gen_from_pred(pred):
  def gen(i):
    i = 0
    while True:
      if pred(i):
        yield i
      i += 1
  return gen
```

Conversely, if we start with a generator:

```python
def gen_even_ints():
  i = 0
  while True:
    yield i
    i += 2
```

a generic recipe for a predicate is:

```python
def pred_from_gen(gen, i):
  def pred(i):
    for j in gen:
      if i == j:
        return True
      if i < j:
        # assumes gen is monotonic
        return False
  return pred
```





# Hierarchy

In the above example, I provided a predicate and generator to define the set of
non-negative even integers.  Such a set symbolized the set of valid inputs to some
hypothetical function to model.




Suppose now we have a function f(x, y) which either succeeds or fails, and we want to
write a schema for it.  




```python
pip install opschema
```

# Unit tests for Tensor Ops

Here I describe an idea and proof of concept PyPI package for generating a
comprenhensive set of unit tests for select Tensor operations.  As with any good unit
test, its goal is to cover all possible code paths leading to success or error, with
at least one representative example for each code path.

The approach will be to use a structure called a *generation graph* to build up the
op inputs one aspect at a time.  A generation graph here is defined as kind of
computation graph, in which each node holds a generator function taking zero or more
arguments.  Each argument is provided in order by the node's parents (if any).  Once
constructed, the graph itself becomes a generator of tuples, with each component of
the tuple coming from one node.


```python
class Node(object):
  def __init__(self, name, gen_func):
    self.name = name
    self.gen_func = gen_func
    self.parents = []
    self.cur_val = None

  def add_parent(self, node):
    self.parents.append(node)

  def __iter__(self):
    # resolve the current values of parent nodes
    pvals = tuple(pa.cur_val for pa in self.parents)

    # instantiate the iterator
    it = self.gen_func(*pvals)
    return it

def iter_graph(*topo_nodes):
  # recursively iterate through all configurations
  names = [n.name for n in topo_nodes]
  vals = [None] * len(topo_nodes)

  def _gen_rec(i):
    if i == len(topo_nodes):
      yield dict(zip(names, vals))
      return
    node = topo_nodes[i]
    for val in iter(node):
      node.cur_val = val
      vals[i] = val
      yield from _gen_rec(i+1)
  
  yield from _gen_rec(0)
```


```python
def gen5():
  yield from range(5)

def gen8(a):
  yield from range(a+1, 8)

n1 = Node('A', gen5)
n2 = Node('B', gen8)
n2.add_parent(n1)

print('\n'.join(str(m) for m in iter_graph(n1, n2)))

```

    {'A': 0, 'B': 1}
    {'A': 0, 'B': 2}
    {'A': 0, 'B': 3}
    {'A': 0, 'B': 4}
    {'A': 0, 'B': 5}
    {'A': 0, 'B': 6}
    {'A': 0, 'B': 7}
    {'A': 1, 'B': 2}
    {'A': 1, 'B': 3}
    {'A': 1, 'B': 4}
    {'A': 1, 'B': 5}
    {'A': 1, 'B': 6}
    {'A': 1, 'B': 7}
    {'A': 2, 'B': 3}
    {'A': 2, 'B': 4}
    {'A': 2, 'B': 5}
    {'A': 2, 'B': 6}
    {'A': 2, 'B': 7}
    {'A': 3, 'B': 4}
    {'A': 3, 'B': 5}
    {'A': 3, 'B': 6}
    {'A': 3, 'B': 7}
    {'A': 4, 'B': 5}
    {'A': 4, 'B': 6}
    {'A': 4, 'B': 7}

