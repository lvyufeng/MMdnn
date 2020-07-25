#----------------------------------------------------------------------------------------------
#  Copyright (c) Microsoft Corporation. All rights reserved.
#  Licensed under the MIT License. See License.txt in the project root for license information.
#----------------------------------------------------------------------------------------------

from mmdnn.conversion.common.DataStructure.graph import GraphNode, Graph
import torch
import torch.jit
import torch.autograd
import torch.serialization
import contextlib
from torch.jit import _unique_state_dict
from torch.onnx.utils import OperatorExportTypes
from torch.onnx.utils import _trace

class scope_name_workaround(object):
    def __init__(self):
        self.backup = None

    def __enter__(self):
        def _tracing_name(self_, tracing_state):
            if not tracing_state._traced_module_stack:
                return None
            module = tracing_state._traced_module_stack[-1]
            for name, child in module.named_children():
                if child is self_:
                    return name
            return None

        def _slow_forward(self_, *input, **kwargs):
            tracing_state = torch._C._get_tracing_state()
            if not tracing_state or isinstance(self_.forward, torch._C.ScriptMethod):
                return self_.forward(*input, **kwargs)
            if not hasattr(tracing_state, '_traced_module_stack'):
                tracing_state._traced_module_stack = []
            name = _tracing_name(self_, tracing_state)
            if name:
                tracing_state.push_scope('%s[%s]' % (self_._get_name(), name))
            else:
                tracing_state.push_scope(self_._get_name())
            tracing_state._traced_module_stack.append(self_)
            try:
                result = self_.forward(*input, **kwargs)
            finally:
                tracing_state.pop_scope()
                tracing_state._traced_module_stack.pop()
            return result
        
        self.backup = torch.nn.Module._slow_forward
        setattr(torch.nn.Module, '_slow_forward', _slow_forward)

    def __exit__(self, type, value, tb):
        setattr(torch.nn.Module, '_slow_forward', self.backup)

class PytorchGraphNode(GraphNode):

    def __init__(self, layer):
        self._name = layer.scopeName()
        self._kind = layer.kind()
        import re
        node_id = re.search(r"[\d]+", layer.__str__())
        self.id = node_id.group(0)

        super(PytorchGraphNode, self).__init__(layer)
        self.attrs = {k : layer[k] for k in layer.attributeNames()}

        self.weights_name = '.'.join(
            re.findall(r'\[([\w\d.]+)\]', self._name)
        )


    @property
    def name(self):
        name = self._name + self.id
        # Scopes created in a nested scope may have initial characters
        # that are illegal as the initial character of an op name
        # (viz. '-', '\', '/', and '_').
        name = name.replace('-','n').replace('\\','n').replace('/','n').replace('_','n').replace('[','n').replace(']','n')
        return name

    @property
    def type(self):
        return self._kind

    @property
    def pytorch_layer(self):
        return self.layer




class PytorchGraph(Graph):

    def __init__(self, model):
        # sanity check.
        super(PytorchGraph, self).__init__(model)
        self.model = model
        self.state_dict = _unique_state_dict(self.model)
        self.shape_dict = dict()

    @staticmethod
    def get_node_id(node):
        import re
        node_id = re.search(r"[\d]+", node.__str__())
        return node_id.group(0)

    @contextlib.contextmanager
    def set_training(self, model, mode):
        r"""
        A context manager to temporarily set the training mode of 'model'
        to 'mode', resetting it when we exit the with-block.  A no-op if
        mode is None.
        """
        if mode is None:
            yield
            return
        old_mode = model.training
        if old_mode != mode:
            model.train(mode)
        try:
            yield
        finally:
            if old_mode != mode:
                model.train(old_mode)


    def build(self, shape):
        """
        build graph for pytorch 0.4.0
        """

        import re
        # construct graph
        dummy_input = torch.autograd.Variable(torch.randn(shape), requires_grad=False)


        # with self.set_training(self.model, False):
        with scope_name_workaround():
            graph = _trace(self.model, dummy_input, OperatorExportTypes.ONNX)

        nodes = list(graph.nodes())


        # input layer
        # TODO



        # build each layer
        for node in nodes:

            node_id = PytorchGraph.get_node_id(node)
            node_scope = node.scopeName()
            node_name = node_scope + node_id
            node_name = node_name.replace('-','n').replace('\\','n').replace('/','n').replace('_','n').replace('[','n').replace(']','n')
            output_shape_str = re.findall(r'[^()!]+', node.__str__())[1]
            output_shape = [int(x.replace('!', '')) for x in output_shape_str.split(',')]


            self.shape_dict[node_name] = output_shape
            self.layer_map[node_name] = PytorchGraphNode(node)
            self.layer_name_map[node_name] = node_name

            # input
            for node_input in list(node.inputs()):

                if PytorchGraph.get_node_id(node_input.node()) and node_input.node().scopeName():
                    node_input_name = node_input.node().scopeName() + PytorchGraph.get_node_id(node_input.node())
                    node_input_name = node_input_name.replace('-','n').replace('\\','n').replace('/','n').replace('_','n').replace('[','n').replace(']','n')
                    self._make_connection(node_input_name, node_name)
                    # print(node_input_name ,'->', node_name)


        super(PytorchGraph, self).build()
