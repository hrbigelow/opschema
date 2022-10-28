"""
Subclasses of OpArg - a class for representing arguments to the op, which are
returned by certain nodes of gen_graph
"""
import enum
import numpy as np
import tensorflow as tf
from .error import SchemaError

class EditType(enum.Enum):
    InsertDim = 0
    DeleteDim = 1
    ChangeDim = 2
    ChangeDType = 3
    ChangeValue = 4

class OpArg(object):
    def __init__(self, *args):
        pass

    def __repr__(self):
        raise NotImplementedError

    def value(self):
        """
        Generate the value for use by the framework op
        """
        raise NotImplementedError

    def edit(self, *args):
        """
        Apply an edit to this arg.  The edit is part of a set of edits (a Fix)
        which should restore all args to a valid set.
        """
        raise NotImplementedError



class DataTensorArg(OpArg):
    """
    An OpArg produced by ge.DataTensor 
    """
    def __init__(self, shape, dtype):
        super().__init__()
        self.shape = shape
        self.dtype = dtype

    def __repr__(self):
        return f'DTen({self.shape}:{self.dtype.name})'

    def value(self):
        try:
            return self._value()
        except BaseException as ex:
            raise SchemaError(
                f'{type(self).__qualname__}: Couldn\'t create value for '
                f'argument with shape \'{self.shape}\' and dtype '
                f'\'{self.dtype.name}\'.  Got exception: '
                f'{ex}')

    def _value(self):
        nelem = np.prod(self.shape)
        # print(repr(self), nelem)
        if nelem > int(1e8):
            raise SchemaError(f'Shape \'{self.shape}\' has {nelem} elements, '
                    f'which exceeds 1e8 elements')
        if self.dtype.is_integer:
            lo = max(self.dtype.min, -1000)
            hi = min(self.dtype.max, 1000) 
            ten = tf.random.uniform(self.shape, lo, hi, dtype=tf.int64)
            ten = tf.cast(ten, self.dtype)
        elif self.dtype.is_floating:
            lo = max(self.dtype.min, -1.0)
            hi = min(self.dtype.max, 1.0)
            ten = tf.random.uniform(self.shape, lo, hi, dtype=tf.float64)
            ten = tf.cast(ten, self.dtype)
        elif self.dtype.is_bool:
            ten = tf.random.uniform(self.shape, 0, 2, dtype=tf.int32)
            ten = tf.cast(ten, self.dtype)
        elif self.dtype.is_quantized:
            lo, hi = -1000, 1000
            ten = tf.random.uniform(self.shape, lo, hi, dtype=tf.float32)
            quant = tf.quantization.quantize(ten, lo, hi, self.dtype)
            ten = quant.output
        elif self.dtype.is_complex:
            lo, hi = -1.0, 1.0
            real = tf.random.uniform(self.shape, lo, hi, dtype=tf.float64)
            imag = tf.random.uniform(self.shape, lo, hi, dtype=tf.float64)
            ten = tf.complex(real, imag, self.dtype)
        else:
            raise SchemaError(
                f'Unexpected dtype when generating tensor: dtype=\'{self.dtype.name}\'')
        return ten

    def edit(self, action, value, index):
        if action == EditType.InsertDim:
            pass

class ShapeTensorArg(OpArg):
    """
    An OpArg produced by ge.ShapeTensor
    """
    def __init__(self, shape):
        super().__init__()
        self.shape = shape

    def value(self):
        return tf.constant(self.shape, dtype=tf.int32)
    
    def __repr__(self):
        return f'ShTen({self.shape})'

class ShapeListArg(OpArg):
    """
    An OpArg produced by ge.ShapeList
    """
    def __init__(self, shape):
        super().__init__()
        self.shape = shape

    def __repr__(self):
        return f'L({self.shape})'

    def value(self):
        return self.shape


"""
OpArgReport classes corresponding to the above OpArg classes
"""

class ShapeTensor2DArg(OpArg):
    """
    An OpArg produced by ge.ShapeTensor2D
    """
    def __init__(self, shape2d):
        self.content = shape2d

    def __repr__(self):
        content = ', '.join(str(r) for r in self.content)
        return f'Sh2Ten({content})'

    def value(self):
        ten = tf.constant(self.content, dtype=tf.int32)
        ten = tf.transpose(ten)
        return ten

class ShapeIntArg(OpArg):
    """
    An OpArg produced by ge.ShapeInt
    """
    def __init__(self, val):
        self.val = val

    def __repr__(self):
        return f'I:{self.val}' 

    def value(self):
        return self.val

class ValueArg(OpArg):
    """
    An OpArg holding an arbitrary value
    """
    def __init__(self, val):
        self.val = val

    def __repr__(self):
        return f'V:{self.val}'

    def value(self):
        return self.val

class OpArgReport(object):
    """
    For generator functions that yield OpArg instances during Test mode, they
    yield instances of OpArgReport in Inference mode.
    """
    def __init__(self, arg_name):
        self.arg_name = arg_name

    def cost(self):
        """
        Returns the total cost of all enclosed edits
        """
        raise NotImplementedError

    def report(self):
        """
        Produce a list of columns in the summary table.  Each column will be a
        list of the following fields: 
        - submitted values
        - interpretation
        - highlight
        """
        raise NotImplementedError

    def oparg(self):
        """
        Construct a corrected OpArg instance
        """
        raise NotImplementedError

class DataTensorReport(OpArgReport):
    def __init__(self, arg_name, shape_edit, dtype_edit): 
        super().__init__(arg_name)
        self.shape_edit = shape_edit
        self.dtype_edit = dtype_edit

    def cost(self):
        return self.shape_edit.cost() + self.dtype_edit.cost()

    def report(self):
        headers = [ f'{self.arg_name}.shape', f'{self.arg_name}.dtype' ]
        shape = self.shape_edit.report()
        shape.insert(0, headers[0])

        if self.dtype_edit is not None:
            highlight = '^' * len(self.dtype_edit.orig_value.name)
        else:
            highlight = ''
        dtype = [headers[1], self.dtype_edit.orig_value.name, '', highlight]
        return [shape, dtype]

    def oparg(self):
        shape = self.shape_edit.apply()
        dtype = self.dtype_edit.apply()
        return DataTensorArg(shape, dtype)

class ShapeTensorReport(OpArgReport):
    def __init__(self, arg_name, shape_edit): 
        super().__init__(arg_name)
        self.shape_edit = shape_edit

    def cost(self):
        return self.shape_edit.cost()

    def report(self):
        header = f'{self.arg_name}'
        rep = self.shape_edit.report()
        rep.insert(0, header)
        return [rep]

    def oparg(self):
        shape = self.shape_edit.apply()
        return ShapeTensorArg(shape)

class ShapeListReport(OpArgReport):
    def __init__(self, arg_name, shape_edit): 
        super().__init__(arg_name)
        self.shape_edit = shape_edit

    def cost(self):
        return self.shape_edit.cost()

    def report(self):
        header = f'{self.arg_name}'
        rep = self.shape_edit.report()
        rep.insert(0, header)
        return [rep]

    def oparg(self):
        shape = self.shape_edit.apply()
        return ShapeListArg(shape)

class ValueReport(OpArgReport):
    def __init__(self, value_edit):
        super().__init__(value_edit.arg_name)
        self.value_edit = value_edit

    def cost(self):
        return self.value_edit.cost()

    def report(self):
        header = f'{self.arg_name}'
        rep = self.value_edit.report()
        return [rep]

    def oparg(self):
        value = self.value_edit.apply()
        return ValueArg(value)
