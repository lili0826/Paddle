#   Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
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

from paddle.utils import gast

from .utils_helper import (
    binary_op_output_type,
    index_in_list,
    is_dygraph_api,
    is_numpy_api,
    is_paddle_api,
    type_from_annotation,
)

__all__ = []


class AstNodeWrapper:
    """
    Wrapper for python gast.node. We need a node wrapper because gast.node
    doesn't store all required information when we are transforming AST.
    We should collect additional information which the actual transformation
    needs.
    """

    def __init__(self, node):
        self.node = node
        self.parent = None
        self.children = []
        self.node_var_type = {"UNKNOWN"}


class StaticAnalysisVisitor:
    """
    A class that does static analysis
    """

    def __init__(self, ast_root=None):
        if ast_root is not None:
            self.run(ast_root)

    def run(self, ast_root):
        self.node_wrapper_root = None
        self.ancestor_wrappers = []
        self.node_to_wrapper_map = {}

        self.dfs_visit(ast_root)

    def dfs_visit(self, node):
        # AST reuses some gast.nodes, such as Param node of expr_context
        if node not in self.node_to_wrapper_map:
            cur_wrapper = AstNodeWrapper(node)
            self.node_to_wrapper_map[node] = cur_wrapper
        else:
            cur_wrapper = self.node_to_wrapper_map[node]

        if self.node_wrapper_root is None:
            self.node_wrapper_root = cur_wrapper

        if len(self.ancestor_wrappers) != 0:
            last_wrapper = self.ancestor_wrappers[-1]
            last_wrapper.children.append(cur_wrapper)
            cur_wrapper.parent = last_wrapper

        self.ancestor_wrappers.append(cur_wrapper)
        for child in gast.iter_child_nodes(node):
            self.dfs_visit(child)
        self.ancestor_wrappers.pop()

        cur_wrapper.node_var_type = self._get_node_var_type(cur_wrapper)
        return cur_wrapper.node_var_type

    def get_node_wrapper_root(self):
        return self.node_wrapper_root

    def get_node_to_wrapper_map(self):
        return self.node_to_wrapper_map

    def is_tensor_node(self, node):
        tensor_types = {"TENSOR", "PADDLE_RETURN_TYPES"}
        node_wrapper = self.node_to_wrapper_map.get(node, None)
        if node_wrapper is None:
            return False
        if node_wrapper.node_var_type & tensor_types:
            return True

    def _get_constant_node_type(self, node):
        assert isinstance(node, gast.Constant), (
            "Type of input node should be gast.Constant, but received %s"
            % type(node)
        )
        # singleton: None, True or False
        if node.value is None:
            return {"NONE"}
        if isinstance(node.value, bool):
            return {"BOOLEAN"}
        if isinstance(node.value, int):
            return {"INT"}
        if isinstance(node.value, float):
            return {"FLOAT"}
        if isinstance(node.value, str):
            return {"STRING"}

        return {"UNKNOWN"}

    def _get_node_var_type(self, cur_wrapper):
        node = cur_wrapper.node
        if isinstance(node, gast.Constant):
            return self._get_constant_node_type(node)

        if isinstance(node, gast.BoolOp):
            return {"BOOLEAN"}
        if isinstance(node, gast.Compare):
            return {"BOOLEAN"}

        if isinstance(node, gast.Dict):
            return {"DICT"}
        if isinstance(node, gast.Set):
            return {"SET"}

        if isinstance(node, gast.UnaryOp):
            return self.node_to_wrapper_map[node.operand].node_var_type

        if isinstance(node, gast.BinOp):
            left_type = self.node_to_wrapper_map[node.left].node_var_type
            right_type = self.node_to_wrapper_map[node.right].node_var_type
            result_type = set()
            for l in left_type:
                for r in right_type:
                    result_type.add(binary_op_output_type(l, r))
            return result_type

        if isinstance(node, gast.Assign):
            ret_type = self.node_to_wrapper_map[node.value].node_var_type
            for target in node.targets:
                if isinstance(target, gast.Name):
                    self.node_to_wrapper_map[target].node_var_type = ret_type
                # Handle statements like `a, b = paddle.shape(x)`
                elif isinstance(target, gast.Tuple):
                    for sub_target in target.elts:
                        if isinstance(sub_target, gast.Name):
                            self.node_to_wrapper_map[
                                sub_target
                            ].node_var_type = ret_type
            return ret_type

        if isinstance(node, gast.AnnAssign):
            # TODO(0x45f): To determine whether need to support assignment statements
            # like `self.x: float = 2.1`.
            ret_type = {type_from_annotation(node.annotation)}
            # if annotation and value(Constant) are diffent type, we use value type
            if node.value:
                node_value_type = self.node_to_wrapper_map[
                    node.value
                ].node_var_type
                if not (node_value_type & {"UNKNOWN", "STATEMENT"}):
                    ret_type = node_value_type
            if isinstance(node.target, gast.Name):
                self.node_to_wrapper_map[node.target].node_var_type = ret_type
            return ret_type

        if isinstance(node, gast.Name):
            if node.id == "None":
                return {"NONE"}
            if node.id in {"True", "False"}:
                return {"BOOLEAN"}
            # If node is child of functionDef.arguments
            parent_node_wrapper = cur_wrapper.parent
            if parent_node_wrapper and isinstance(
                parent_node_wrapper.node, gast.arguments
            ):
                return self._get_func_argument_type(parent_node_wrapper, node)

            return {"UNKNOWN"}

        if isinstance(node, gast.Return):
            # If return nothing:
            if node.value is None:
                return {"NONE"}

            return {"UNKNOWN"}

        if isinstance(node, gast.Call):
            if is_dygraph_api(node):
                if isinstance(node.func, gast.Attribute):
                    if node.func.attr == "to_variable":
                        return {"TENSOR"}
            if is_paddle_api(node):
                return {"PADDLE_RETURN_TYPES"}
            if is_numpy_api(node):
                # In this simple version we assume numpy api returns nd-array
                return {"NUMPY_NDARRAY"}

            if isinstance(node.func, gast.Name):
                return {"UNKNOWN"}
        if isinstance(node, gast.Subscript):
            if self.is_tensor_node(node.value):
                return {"TENSOR"}

        return {"STATEMENT"}

    def _get_func_argument_type(self, parent_node_wrapper, node):
        """
        Returns type information by parsing annotation or default values.

        For example:
            1. parse by default values.
                foo(x, y=1, z='s') -> x: UNKNOWN, y: INT, z: STR

            2. parse by Py3 type annotation.
                foo(x: Tensor, y: int, z: str) -> x: Tensor, y: INT, z: STR

            3. parse by type annotation and default values.
                foo(x: Tensor, y: int, z: str = 'abc') -> x: Tensor, y: INT, z: STR

        NOTE: Currently, we only support Tensor, int, bool, float, str et.al.
              Other complicate types will be supported later.
        """
        assert isinstance(node, gast.Name)

        parent_node = parent_node_wrapper.node
        var_type = {"UNKNOWN"}
        if node.annotation is not None:
            var_type = {type_from_annotation(node.annotation)}

        # if annotation and value(Constant) are diffent type, we use value type
        if parent_node.defaults:
            index = index_in_list(parent_node.args, node)
            args_len = len(parent_node.args)
            if index != -1 and args_len - index <= len(parent_node.defaults):
                defaults_node = parent_node.defaults[index - args_len]
                if isinstance(defaults_node, gast.Constant):
                    var_type = self._get_constant_node_type(defaults_node)

        return var_type
