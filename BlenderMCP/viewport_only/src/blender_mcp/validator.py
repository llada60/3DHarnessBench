"""AST allowlist restricting execute_blender_code to camera/viewport UI state.

The guard lives entirely on the MCP-server side: the viewport_only addon runs
whatever it receives with a plain exec(), so validate_camera_view_code() must
reject anything outside this deliberately tiny subset before it reaches
Blender.
"""
import ast
from typing import NoReturn

_CAMERA_OBJECT_PROPERTIES = {
    "location",
    "matrix_world",
    "rotation_euler",
    "rotation_mode",
    "rotation_quaternion",
}
_CAMERA_DATA_PROPERTIES = {
    "clip_end",
    "clip_start",
    "lens",
    "ortho_scale",
    "sensor_fit",
    "sensor_height",
    "sensor_width",
    "shift_x",
    "shift_y",
    "type",
}
_VIEWPORT_PROPERTIES = {
    "lock_rotation",
    "view_camera_offset",
    "view_camera_zoom",
    "view_distance",
    "view_location",
    "view_matrix",
    "view_perspective",
    "view_rotation",
}
_SHADING_TYPES = {"WIREFRAME", "SOLID", "MATERIAL", "RENDERED"}
_VIEW_PERSPECTIVES = {"PERSP", "ORTHO", "CAMERA"}

# Semantic types for the deliberately tiny Blender UI object graph exposed to
# the validator.  An attribute not present here is not merely "unknown": it is
# outside the viewport-only capability and is rejected before code reaches
# Blender.  Keeping this graph explicit prevents an allowed UI object from
# becoming a bridge to arbitrary RNA/Python attributes.
_UI_ATTRIBUTE_KINDS: dict[str, dict[str, str]] = {
    "bpy": {"context": "context"},
    "context": {
        "area": "viewport_area",
        "screen": "screen",
        "space_data": "viewport_space",
        "window": "window",
        "workspace": "workspace",
        # Camera access remains the only permitted route through scene.
        "scene": "camera_scene",
    },
    "window": {"screen": "screen", "workspace": "workspace"},
    "camera_scene": {"camera": "camera_object"},
    "camera_object": {
        "data": "camera_data",
        **{name: "value" for name in _CAMERA_OBJECT_PROPERTIES},
    },
    "camera_data": {name: "value" for name in _CAMERA_DATA_PROPERTIES},
    "screen": {"areas": "areas"},
    "viewport_area": {
        "regions": "regions",
        "spaces": "spaces",
        "tag_redraw": "area_tag_redraw",
        "type": "value",
    },
    "regions": {},
    "region": {"height": "value", "type": "value", "width": "value"},
    "spaces": {"active": "viewport_space"},
    "viewport_space": {
        "overlay": "viewport_overlay",
        "region_3d": "region_3d",
        "shading": "viewport_shading",
        "type": "value",
    },
    "viewport_shading": {"type": "value"},
    "viewport_overlay": {"show_overlays": "value"},
    "region_3d": {
        "update": "region_3d_update",
        **{name: "value" for name in _VIEWPORT_PROPERTIES},
    },
    "workspace": {
        "name": "value",
        "status_text_set": "workspace_status_text_set",
    },
}
_ITERABLE_ELEMENT_KINDS = {
    "areas": "viewport_area",
    "regions": "region",
    "spaces": "viewport_space",
}
_CALLABLE_KINDS = {
    "area_tag_redraw",
    "constructor",
    "math_callable",
    "mathutils_constructor",
    "region_3d_update",
    "workspace_status_text_set",
}


class _CameraViewCodeValidator(ast.NodeVisitor):
    """Accept only a small Python subset that can change camera/viewport UI."""

    def __init__(self) -> None:
        self.aliases: dict[str, str] = {
            "bpy": "bpy",
            "math": "math_module",
            "mathutils": "mathutils_module",
            "Euler": "constructor",
            "Matrix": "constructor",
            "Quaternion": "constructor",
            "Vector": "constructor",
        }
        self.has_camera_view_mutation = False

    def reject(self, node: ast.AST, reason: str) -> NoReturn:
        raise ValueError(f"line {getattr(node, 'lineno', '?')}: {reason}")

    def value_kind(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self.value_kind(node.value)
            if base == "math_module" and not node.attr.startswith("_"):
                return "math_callable"
            if base == "mathutils_module" and node.attr in {
                "Euler", "Matrix", "Quaternion", "Vector"
            }:
                return "mathutils_constructor"
            return _UI_ATTRIBUTE_KINDS.get(base or "", {}).get(node.attr)
        if isinstance(node, ast.Subscript):
            return _ITERABLE_ELEMENT_KINDS.get(self.value_kind(node.value) or "")
        if isinstance(node, (ast.Constant, ast.List, ast.Tuple, ast.Set, ast.Dict,
                             ast.BinOp, ast.BoolOp, ast.Compare, ast.IfExp,
                             ast.UnaryOp)):
            return "local_value"
        if isinstance(node, ast.Call):
            kind = self.value_kind(node.func)
            if kind in {"constructor", "math_callable", "mathutils_constructor"}:
                return "local_value"
        return None

    @staticmethod
    def expression_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return f"{_CameraViewCodeValidator.expression_name(node.value)}.{node.attr}"
        if isinstance(node, ast.Subscript):
            return f"{_CameraViewCodeValidator.expression_name(node.value)}[...]"
        return type(node).__name__

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            if item.name not in {"bpy", "math", "mathutils"} or item.asname:
                self.reject(node, "only unaliased bpy, math, and mathutils imports are allowed")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        allowed = {"Euler", "Matrix", "Quaternion", "Vector"}
        if (
            node.module != "mathutils"
            or any(item.name not in allowed or item.asname for item in node.names)
        ):
            self.reject(node, "only unaliased mathutils camera transform types may be imported")

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and node.id not in self.aliases:
            self.reject(node, f"name {node.id!r} is not an allowed import or local value")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_"):
            self.reject(node, "private and special attributes are not allowed")
        if self.value_kind(node) is None:
            self.reject(
                node,
                f"attribute chain {self.expression_name(node)!r} is outside the "
                "camera/viewport UI allowlist",
            )
        self.visit(node.value)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        collection = self.value_kind(node.value)
        if collection not in _ITERABLE_ELEMENT_KINDS:
            self.reject(node, "subscripting is only allowed on viewport UI collections")
        if not isinstance(node.slice, ast.Constant) or not isinstance(node.slice.value, int):
            self.reject(node, "viewport UI collections require a literal integer index")
        self.visit(node.value)

    def visit_Call(self, node: ast.Call) -> None:
        kind = self.value_kind(node.func)
        if kind not in _CALLABLE_KINDS:
            self.reject(
                node,
                f"call to {self.expression_name(node.func)!r} is not allowed",
            )
        if kind in {"area_tag_redraw", "region_3d_update"} and (
            node.args or node.keywords
        ):
            self.reject(node, "viewport update/redraw calls take no arguments")
        if kind == "workspace_status_text_set" and (
            node.keywords
            or len(node.args) != 1
            or not (
                isinstance(node.args[0], ast.Constant)
                and (node.args[0].value is None or isinstance(node.args[0].value, str))
            )
        ):
            self.reject(node, "workspace.status_text_set requires one literal string or None")
        if kind in {"area_tag_redraw", "region_3d_update", "workspace_status_text_set"}:
            self.has_camera_view_mutation = True
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        self.reject(node, "deletion is not allowed")

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.reject(node, "assignment expressions are not allowed")

    def visit_Expr(self, node: ast.Expr) -> None:
        if not isinstance(node.value, ast.Call):
            self.reject(node, "only viewport redraw/update calls may be standalone expressions")
        kind = self.value_kind(node.value.func)
        if kind not in {
            "area_tag_redraw", "region_3d_update", "workspace_status_text_set"
        }:
            # Validate the call first so dangerous/unknown functions produce a
            # precise reason instead of the less useful expression-level one.
            self.visit(node.value)
            self.reject(node, "only viewport redraw/update calls may be standalone expressions")
        self.visit(node.value)

    def visit_FunctionDef(self, node: ast.AST) -> None:
        self.reject(node, "function definitions are not allowed")

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.AST) -> None:
        self.reject(node, "class definitions are not allowed")

    def visit_Lambda(self, node: ast.AST) -> None:
        self.reject(node, "lambda expressions are not allowed")

    def visit_While(self, node: ast.AST) -> None:
        self.reject(node, "while loops are not allowed")

    def visit_With(self, node: ast.AST) -> None:
        self.reject(node, "context managers are not allowed")

    visit_AsyncWith = visit_With

    def visit_Try(self, node: ast.AST) -> None:
        self.reject(node, "try statements are not allowed")

    visit_TryStar = visit_Try

    def visit_Raise(self, node: ast.AST) -> None:
        self.reject(node, "raise statements are not allowed")

    def visit_Assert(self, node: ast.AST) -> None:
        self.reject(node, "assert statements are not allowed")

    def visit_Global(self, node: ast.AST) -> None:
        self.reject(node, "global statements are not allowed")

    visit_Nonlocal = visit_Global

    def visit_Await(self, node: ast.AST) -> None:
        self.reject(node, "await expressions are not allowed")

    def visit_Yield(self, node: ast.AST) -> None:
        self.reject(node, "yield expressions are not allowed")

    visit_YieldFrom = visit_Yield

    def visit_ListComp(self, node: ast.AST) -> None:
        self.reject(node, "comprehensions are not allowed")

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_For(self, node: ast.For) -> None:
        collection = self.value_kind(node.iter)
        element = _ITERABLE_ELEMENT_KINDS.get(collection or "")
        if not isinstance(node.target, ast.Name) or element is None:
            self.reject(
                node,
                "loops may only iterate over viewport areas, spaces, or regions",
            )
        self.visit(node.iter)
        self.aliases[node.target.id] = element
        for statement in node.body + node.orelse:
            self.visit(statement)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.reject(node, "async loops are not allowed")

    def _record_assignment(self, target: ast.AST, value: ast.AST) -> None:
        if isinstance(target, ast.Name):
            kind = self.value_kind(value)
            if kind in _CALLABLE_KINDS:
                self.reject(value, "callable aliases are not allowed")
            self.aliases[target.id] = kind or "local_value"
            return

        if not isinstance(target, ast.Attribute):
            self.reject(target, "only local names and camera-view properties may be assigned")

        owner = self.value_kind(target.value)
        if target.attr.startswith("_"):
            self.reject(target, "private and special attributes are not allowed")
        if owner is None:
            self.reject(
                target,
                f"assignment target {self.expression_name(target)!r} is outside "
                "the camera/viewport UI allowlist",
            )
        allowed = (
            (owner == "camera_object" and target.attr in _CAMERA_OBJECT_PROPERTIES)
            or (owner == "camera_data" and target.attr in _CAMERA_DATA_PROPERTIES)
            or (owner == "region_3d" and target.attr in _VIEWPORT_PROPERTIES)
            or (owner == "viewport_shading" and target.attr == "type")
            or (owner == "viewport_overlay" and target.attr == "show_overlays")
        )
        if not allowed:
            self.reject(
                target,
                "assignment does not target an allowed camera or viewport UI property",
            )
        if owner == "viewport_shading" and not (
            isinstance(value, ast.Constant) and value.value in _SHADING_TYPES
        ):
            self.reject(value, f"shading.type must be one of {sorted(_SHADING_TYPES)}")
        if owner == "viewport_overlay" and not (
            isinstance(value, ast.Constant) and isinstance(value.value, bool)
        ):
            self.reject(value, "overlay.show_overlays must be a literal boolean")
        if owner == "region_3d" and target.attr == "view_perspective" and not (
            isinstance(value, ast.Constant) and value.value in _VIEW_PERSPECTIVES
        ):
            self.reject(
                value,
                f"view_perspective must be one of {sorted(_VIEW_PERSPECTIVES)}",
            )
        self.has_camera_view_mutation = True

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._record_assignment(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is None:
            self.reject(node, "annotation-only assignments are not allowed")
        self.visit(node.value)
        self._record_assignment(node.target, node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self._record_assignment(node.target, node.value)


def validate_camera_view_code(code: str) -> None:
    """Raise unless *code* only modifies an allowed camera/viewport UI state."""
    if not code or not code.strip():
        raise ValueError("code must not be empty")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise ValueError(f"invalid Python syntax: {exc.msg}") from exc

    validator = _CameraViewCodeValidator()
    validator.visit(tree)
    if not validator.has_camera_view_mutation:
        raise ValueError("code must modify the active camera or 3D viewport view")
