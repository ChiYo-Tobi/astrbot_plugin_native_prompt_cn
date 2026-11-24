"""Versioned expression patches. Never match against assembled messages."""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import json
import re
from pathlib import Path
from types import CodeType

ROOT = Path(__file__).parent
DEFAULTS = json.loads((ROOT / 'defaults.json').read_text(encoding='utf-8'))
TOKEN = re.compile(r'\{([a-z][a-z_0-9]*)\}')
REQUIRED = {
    'workspace': {'workspace'}, 'workspace_extra_source': {'path', 'text'},
    'local': {'os', 'shell'}, 'skill_row': {'name', 'description', 'path'},
    'skills': {'skills', 'example_command'}, 'background': {'background_task_result'},
}
HELPER = '_native_prompt_cn_render_v1'

# file module, function (including class if applicable), source locations and arguments.
def functions(tree):
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for fn in node.body:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f'{node.name}.{fn.name}'] = fn
    return out


def fingerprint(node):
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()


def code_objects(code):
    for child in code.co_consts:
        if isinstance(child, CodeType):
            yield child.co_qualname, child
            yield from code_objects(child)


class Rewrite(ast.NodeTransformer):
    def __init__(self, specs):
        self.specs = specs
        self.hits = []

    def visit(self, node):
        if not isinstance(node, ast.expr):
            return super().visit(node)
        spec = self.specs.get(id(node))
        if not spec:
            return super().visit(node)
        key, kind, args = spec
        match = (
            (kind == 'constant' and isinstance(node, ast.Constant) and isinstance(node.value, str))
            or (kind == 'name' and isinstance(node, ast.Name) and node.id.isupper())
            or (kind in {'joined','heading','header'} and isinstance(node, ast.JoinedStr))
            or (kind == 'background' and isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == 'BACKGROUND_TASK_RESULT_WOKE_SYSTEM_PROMPT')
        )
        if not match:
            return super().visit(node)
        self.hits.append(id(node))
        values = {k: ast.Name(id=v, ctx=ast.Load()) for k,v in args.items()}
        original = copy.deepcopy(node)
        if kind in {'header', 'heading'}:
            index = 1 if kind == 'header' else 0
            original = copy.deepcopy(node.values[index])
        if kind == 'background':
            # Original is already rendered, including braces in the arbitrary JSON value.
            original = copy.deepcopy(node)
        rendered = ast.Call(func=ast.Name(id=HELPER, ctx=ast.Load()), args=[ast.Constant(key), original],
                            keywords=[ast.keyword(arg=k, value=v) for k,v in values.items()])
        if kind in {'header','heading'}:
            node.values[index] = ast.copy_location(ast.FormattedValue(value=rendered, conversion=-1), node.values[index])
            return node
        return ast.copy_location(rendered, node)


class Patches:
    def __init__(self, config, logger):
        self.config = config
        self.logger = logger
        self.saved = []
        self.namespaces = []
        self.warned = set()
        self.active_keys = set()
        self.skipped = {}

    def render(self, key, original, **values):
        if not self.config.get('enabled', True):
            return original
        override = self.config.get(key)
        if override == '@original':
            return original
        default = DEFAULTS[key][1]
        text = override if isinstance(override, str) and override.strip() else default
        tokens = set(TOKEN.findall(text))
        if REQUIRED.get(key, set()) - tokens or tokens - values.keys():
            if key not in self.warned:
                self.warned.add(key)
                self.logger.warning('原生提示词 %s 的占位符有误，保留原文', key)
            return original
        return TOKEN.sub(lambda match: str(values[match[1]]), text)

    def install(self):
        if self.saved:
            return
        manifest = json.loads((ROOT / 'fingerprints.json').read_text(encoding='utf-8'))
        self.skipped.clear()
        self.active_keys.clear()
        pending = []
        namespaces = []
        for module_name, matches in manifest.items():
            try:
                module = importlib.import_module('astrbot.core.' + module_name)
                namespace = vars(module)
                if HELPER in namespace and not getattr(namespace[HELPER], '_native_prompt_cn_identity', False):
                    raise RuntimeError('已有同名运行时钩子')
                filename = module.__file__
                tree = ast.parse(Path(filename).read_text(encoding='utf-8'), filename=filename)
                nodes = functions(tree)
                originals = dict(code_objects(compile(tree, filename, 'exec', dont_inherit=True)))
            except Exception as exc:
                for name in matches:
                    self.skipped[f'{module_name}.{name}'] = f'模块不可用 ({type(exc).__name__})'
                continue
            changed = []
            for qualname, spec in matches.items():
                try:
                    node = nodes.get(qualname)
                    if node is None or fingerprint(node) != spec['sha256']:
                        raise ValueError('组装函数已变更或不存在')
                    target = module
                    for part in qualname.split('.'):
                        target = getattr(target, part)
                    fn = getattr(target, '__func__', target)
                    old = fn.__code__
                    if old != originals[qualname]:
                        raise ValueError('运行时实现与磁盘源码不一致')
                    # The structural hash pins the AST walk order. Moving the function
                    # or adding blank lines/comments does not change its site indices.
                    candidate = copy.deepcopy(node)
                    walk = list(ast.walk(candidate))
                    rules = {id(walk[int(index)]): rule for index, rule in spec['sites'].items()}
                    rewrite = Rewrite(rules)
                    rewrite.visit(candidate)
                    if sorted(rewrite.hits) != sorted(rules):
                        raise ValueError('组装点缺失或重复')
                    node.body = candidate.body
                    changed.append((qualname, fn, old, spec))
                except (AttributeError, KeyError, TypeError, IndexError, ValueError) as exc:
                    self.skipped[f'{module_name}.{qualname}'] = str(exc)
            if not changed:
                continue
            try:
                codes = dict(code_objects(compile(ast.fix_missing_locations(tree), filename, 'exec', dont_inherit=True)))
            except (TypeError, ValueError, SyntaxError) as exc:
                for qualname, *_ in changed:
                    self.skipped[f'{module_name}.{qualname}'] = f'补丁编译失败 ({type(exc).__name__})'
                continue
            module_pending = []
            for qualname, fn, old, spec in changed:
                new = codes.get(qualname)
                if new is None or old.co_freevars != new.co_freevars:
                    self.skipped[f'{module_name}.{qualname}'] = '补丁实现不兼容'
                    continue
                module_pending.append((fn, old, new))
                self.active_keys.update(rule[0] for rule in spec['sites'].values())
            if module_pending:
                pending.extend(module_pending)
                namespaces.append(namespace)
        renderer = self.render
        try:
            for namespace in namespaces:
                namespace[HELPER] = renderer
                self.namespaces.append((namespace, renderer))
            for fn, old, new in pending:
                fn.__code__ = new
                self.saved.append((fn, old, new))
        except BaseException:
            self.uninstall()
            raise

    def uninstall(self):
        for fn, old, new in reversed(self.saved):
            if fn.__code__ is new:
                fn.__code__ = old
        self.saved.clear()
        self.active_keys.clear()
        # Suspended coroutine frames still run the old code they started with.
        # Leave an identity fallback for them; the next install can adopt it.
        for namespace, renderer in self.namespaces:
            if namespace.get(HELPER) is renderer:
                namespace[HELPER] = _original
        self.namespaces.clear()


def _original(key, original, **values):
    return original

_original._native_prompt_cn_identity = True
