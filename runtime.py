import tensorflow as tf
import numpy as np
import itertools
import re
from parse import BCParser

def equal_tens(a, b, eps):
    if not a.dtype.is_floating:
        eps = 0
    return (
            a.shape == b.shape and
            tf.reduce_all(tf.less_equal(tf.abs(a - b), eps)).numpy()
            )

class Shape(object):
    # simple data class
    def __init__(self):
        self.dims = None

    def set(self, dims):
        self.dims = [ int(d) for d in dims ]

    def set_elem(self, ind, dim):
        if self.dims is None:
            raise RuntimeError('Cannot call set_elem() on uninitialized Shape')
        if ind >= len(self.dims):
            raise RuntimeError(
                f'set_elem() index {ind} out of bounds for length '
                f'{len(self.dims)} dims')
        self.dims[ind] = dim

    def get(self):
        if self.dims is None:
            raise RuntimeError('Cannot call get() on uninitialized Shape')
        return self.dims


class EinTup(object):
    def __init__(self, name, shadow_of=None):
        self.name = name
        self.shadow_of = shadow_of
        self.shape = Shape() if shadow_of is None else shadow_of.shape
        self._value = None

    def __repr__(self):
        try:
            dimstring = ','.join([str(d) for d in self.dims()])
        except RuntimeError:
            dimstring = '?'
        shadow = ''
        if not self.primary():
            shadow = f'(shadowing {self.shadow_of.name})'
        return f'EinTup \'{self.name}\': [{dimstring}]'

    def __len__(self):
        return len(self.dims())

    def __iter__(self):
        self.index = np.ndindex(*self.dims())
        return self

    def __next__(self):
        # intentionally silent.  simply used to advance the position
        self._value = next(self.index)
        return self.value()
    
    def primary(self):
        return self.shadow_of is None

    def same_shape_as(self, other):
        return self.shape is other.shape 

    def dims(self):
        return self.shape.get()

    def rank(self):
        return len(self.shape.get())

    def nelem(self):
        return np.prod(self.dims(), dtype=np.int32)

    def set_dims(self, dims):
        if self.shadow_of is not None:
            raise RuntimeError(f'cannot call set_dims on shadowing EinTup')
        self.shape.set(dims)

    def set_dim(self, ind, val):
        self.shape.set_elem(ind, val)

    def value(self):
        if self._value is None:
            raise RuntimeError(f'{self} called value() before iteration')
        return self._value


class Runtime(object):
    def __init__(self, min_dim=5, max_dim=100):
        self.parser = BCParser() 
        # map of eintup names to EinTup instances
        self.tups = {}

        # defines the signature (see notes.txt) of arrays
        self.array_sig = {}

        # stores current values of the arrays.  the shape
        # either matches or is broadcastable to the signature
        self.arrays = {}

        # The program statement top-level AST nodes
        self.statements = None 

        # Ast nodes representing rank and dim comstraints
        self.constraints = None

        self.min_dim = min_dim
        self.max_dim = max_dim
        self.parser.set_runtime(self)

    def __repr__(self):
        tups = 'Tups: \n' + '\n'.join(repr(tup) for tup in self.tups.values())

        sigs = 'Array Signatures: \n' 
        sigs += '\n'.join(name + ': ' + repr(sig) for name, sig in
                self.array_sig.items())

        shapes = 'Array Shapes: \n'
        shapes += '\n'.join(name + ': ' + repr(ary.shape) 
                for name, ary in self.arrays.items())

        statements = 'Statements: \n'
        statements += '\n'.join(repr(st) for st in self.statements)

        tfcall = 'TF Call: \n'
        tfcall += repr(self.tf_call)

        out_args = 'Output Args: \n'
        out_args += repr(self.out_args)

        return (f'{tups}\n\n{sigs}\n\n{shapes}\n\n{statements}\n\n'
                f'{tfcall}\n\n{out_args}\n')

    def parse_et_file(self, et_file):
        with open(et_file, 'r') as fh:
            content = fh.read()

        sections = iter(re.split('\n\n+', content.strip()))
        statements = next(sections)
        tf_call = next(sections)
        tf_output = next(sections)
        constraints = next(sections, '')

        statements = statements.strip().split('\n')
        tf_call = tf_call.replace('\n', ' ').strip()
        tf_output = tf_output.replace('\n', ' ').strip()
        constraints = constraints.strip().split('\n')

        self.parser.set_statement_mode()
        self.statements = [ self.parser.parse(st) for st in statements ]

        self.parser.set_tfcall_mode()
        self.tf_call = self.parser.parse(tf_call)

        # ordered list of TensorArg nodes in the order matching expected tf
        # output
        self.parser.set_output_mode()
        self.out_args = self.parser.parse(tf_output)
        
        self.parser.set_constraint_mode()
        self.constraints = [ self.parser.parse(con) for con in constraints ]

        # post-init all AST nodes
        all_nodes = self.statements + self.constraints + [self.tf_call]
        for node in all_nodes: 
            node.post_parse_init()
        
    # run the full program and produce the set of output tensors in the
    # preconfigured order
    def run(self):
        if not all(con.value() for con in self.constraints):
            return None
        for st in self.statements:
            st.evaluate()
        outs = { (arg.name, arg.value()) for arg in self.outputs }
        return outs

    # generate all qualifying rank + dims combinations as a rank_map plus
    # dims_map pair
    def gen_shapes(self):
        primary_tup_names = [ tup.name for tup in self.get_primary_tups() ]
        rank_space = (10,) * len(primary_tup_names)
        for rank_combo in np.ndindex(rank_space):
            rank_map = dict(zip(primary_tup_names, rank_combo))
            self.set_ranks(rank_map)
            if not all(con.value() for con in self.constraints):
                continue
            yield rank_map

    def validate_all(self):
        for rmap in self.gen_shapes():
            valid = self.validate()
            print(f'{rmap} {valid}')

    # validate the current rank + dims setting
    def validate(self):
        for st in self.statements:
            st.evaluate()
        tf_outputs = self.tf_call.value()
        z = zip(self.out_args, tf_outputs)
        valid = [ equal_tens(et.value(), tf_out, 1e-6) for et, tf_out in z ]
        return valid

    def set_ranks(self, rank_map):
        for tup, rank in rank_map.items():
            if tup not in self.tups:
                raise RuntimeError(
                    f'Cannot set dims for unknown EinTup {tup}')
            if not self.tup(tup).primary():
                raise RuntimeError(
                    f'Cannot set dims for non-primary EinTup {tup}')
            dims = np.random.randint(self.min_dim, self.max_dim, rank)
            self.tup(tup).set_dims(dims)

    def set_dims(self, dims_map):
        for name, dims in dims_map.items():
            self.tups[name].set_dims(dims)


    def set_one_dim(self, tup, ind, val):
        self.tup(tup).set_dim(ind, val)

    def maybe_add_tup(self, name, shadow_of=None):
        if name in self.tups:
            pass
        elif shadow_of is None:
            self.tups[name] = EinTup(name, None)
        elif shadow_of.name in self.tups:
            self.tups[name] = EinTup(name, shadow_of)
        else:
            raise RuntimeError(
                f'Runtime::maybe_add_tup - shadow_of \'{shadow_of}\' '
                f'provided but does not exist')
        return self.tups[name]

    def get_primary_tups(self):
        return [ tup for tup in self.tups.values() if tup.primary() ]

    def tup(self, eintup):
        if eintup not in self.tups:
            raise RuntimeError(
                    f'Runtime::tup() got unknown eintup name {eintup}')
        return self.tups[eintup]

    def dims(self, eintup):
        return self.tup(eintup).dims()

    def rank(self, eintup):
        return len(self.dims(eintup))

    def nelem(self, eintup):
        return self.tup(eintup).nelem()

    def cycle(self, *eintups):
        tups = [self.tup(e) for e in eintups]
        return itertools.product(*tups)

if __name__ == '__main__':
    rt = Runtime()
    rt.set_ranks({'batch': 2, 'slice': 1})
    for val in rt.cycle('slice'):
        print(val)
