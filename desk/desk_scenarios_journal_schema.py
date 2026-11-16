import ast
from pathlib import Path

from .desk_journal_schema import COMMON_FIELDS, EVENT_FIELDS, PRICE_FIELDS, REPLY_FIELDS


def literal_strings(node):
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return {node.value}
    if isinstance(node, ast.IfExp):
        return literal_strings(node.body) | literal_strings(node.orelse)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return set().union(*(literal_strings(value) for value in node.elts))
    raise AssertionError('Unresolved journal string: ' + ast.unparse(node))


def call_name(node):
    return node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')


def check_writer_fields(sources=None):
    directory = Path(__file__).parent
    sources = sources or {path.name: path.read_text() for path in directory.glob('*.py')
                          if not path.name.startswith('desk_scenarios')}
    trees = {name: ast.parse(source) for name, source in sources.items()}
    counts = 0
    wrapper_fields = {'set_state': set(), 'emit': set(), 'write_delivery': set()}
    calls = []
    for filename, tree in trees.items():
        aliases = {alias.asname or alias.name: ('append_record' if alias.name == 'append_delivery_record' else alias.name)
                   for item in ast.walk(tree) if isinstance(item, ast.ImportFrom)
                   for alias in item.names if alias.name in ('append_record', 'append_delivery_record', 'write_delivery',
                                                            'set_state', 'emit', 'write_row')}
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            original_node = node
            name = aliases.get(call_name(node), call_name(node))
            if name == 'to_thread' and isinstance(node.args[0], ast.Name):
                name = aliases.get(node.args[0].id, node.args[0].id)
                node = ast.Call(func=ast.Name(id=name), args=node.args[1:], keywords=node.keywords)
            if name not in ('append_record', 'diagnostic_record', 'set_state', 'emit', 'write_delivery', 'write_row'):
                continue
            parent = parents.get(original_node)
            while parent is not None and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parent = parents.get(parent)
            calls.append((filename, name, node, parent, tree))
            if name in wrapper_fields:
                wrapper_fields[name].update(keyword.arg for keyword in node.keywords if keyword.arg)
    for filename, name, node, parent, tree in calls:
        if name == 'set_state':
            events = {'state'}
        else:
            event = node.args[1]
            if isinstance(event, ast.Name) and event.id == 'event':
                if filename == 'desk_coordinator.py':
                    events = {'reply', 'prepare_reply', 'backoff', 'nonce_retry'}
                elif filename in ('desk_delivery_store.py', 'desk_deliveries.py'):
                    events = {'delivery_pending', 'delivery_reported', 'delivery_abandoned',
                              'deliveries_disabled', 'delivery_error'}
                else:
                    raise AssertionError((filename, ast.unparse(node)))
                assignments = [item.value for item in ast.walk(parent or tree) if isinstance(item, ast.Assign)
                               and any(isinstance(target, ast.Name) and target.id == 'event' for target in item.targets)]
                if assignments:
                    events = set().union(*(literal_strings(value) for value in assignments))
            else:
                events = literal_strings(event)
        for event in events:
            assert event in EVENT_FIELDS, (filename, event)
        scope = parent or tree

        def fields(value):
            if isinstance(value, ast.Dict):
                result = set()
                for key, item in zip(value.keys, value.values):
                    result.update(fields(item) if key is None else literal_strings(key))
                return result
            if isinstance(value, ast.IfExp):
                return fields(value.body) | fields(value.orelse)
            if isinstance(value, ast.Name):
                if value.id == 'prices':
                    function = next(item for item in ast.walk(trees['desk_io.py'])
                                    if isinstance(item, ast.AsyncFunctionDef) and item.name == 'oracle_prices')
                    keys = set()
                    returned = [item.value for item in ast.walk(function) if isinstance(item, ast.Return)]
                    returned.extend(item.value for item in ast.walk(function) if isinstance(item, ast.Assign)
                                    and any(isinstance(target, ast.Name) and target.id == 'result' for target in item.targets))
                    for item in returned:
                        if isinstance(item, ast.Dict):
                            for key in item.keys:
                                if key is None:
                                    continue
                                if isinstance(key, ast.BinOp) and isinstance(key.left, ast.Name) and key.left.id == 'source':
                                    keys.update(source + suffix for source in ('hl_oracle', 'binance')
                                                for suffix in literal_strings(key.right))
                                else:
                                    keys.update(literal_strings(key))
                    assert keys == set(PRICE_FIELDS), keys
                    return keys
                if value.id in ('values', 'fields'):
                    if filename == 'desk_record.py':
                        return wrapper_fields['set_state']
                    if filename == 'desk_coordinator.py':
                        return wrapper_fields['emit'] | set(REPLY_FIELDS)
                    if filename == 'desk_delivery_store.py' or filename == 'desk_deliveries.py' and scope.name == 'persist_delivery':
                        return wrapper_fields['write_delivery'] | {'signal_id', 'event_id'}
                assignments = [item.value for item in ast.walk(scope) if isinstance(item, ast.Assign)
                               and any(isinstance(target, ast.Name) and target.id == value.id for target in item.targets)]
                assert assignments, (filename, ast.unparse(value))
                return set().union(*(fields(item) for item in assignments))
            if isinstance(value, ast.DictComp):
                if filename == 'signer_delegate_client.py':
                    assert ast.unparse(value) == '{name: value for name, value in result.items() if name in REPLY_JOURNAL_FIELDS}'
                    declaration = next(item.value for item in tree.body if isinstance(item, ast.Assign)
                                       and any(isinstance(target, ast.Name) and target.id == 'REPLY_JOURNAL_FIELDS'
                                               for target in item.targets))
                    return literal_strings(declaration)
                if filename == 'desk_deliveries.py':
                    assert ast.unparse(value.key) == 'name'
                    return literal_strings(value.generators[0].iter)
            raise AssertionError((filename, 'Unresolved journal fields', ast.unparse(value)))

        names = set()
        for keyword in node.keywords:
            names.update({keyword.arg} if keyword.arg else fields(keyword.value))
        if name == 'write_row':
            names.update(fields(node.args[2]))
        for event in events:
            allowed = set(EVENT_FIELDS[event]) | set(COMMON_FIELDS)
            if isinstance(node.args[1], ast.Name) and node.args[1].id == 'event' and len(events) > 1:
                allowed = set().union(*(EVENT_FIELDS[item] for item in events)) | set(COMMON_FIELDS)
            assert names <= allowed, (filename, event, sorted(names - allowed))
        counts += 1
    assert counts >= 40, counts
    return counts


def check_static_coverage():
    count = check_writer_fields()
    directory = Path(__file__).parent
    sources = {path.name: path.read_text() for path in directory.glob('*.py')
               if not path.name.startswith('desk_scenarios')}
    mutations = [('desk_executor.py', 'reason=reason)', 'reason=reason, uncovered_field=True)'),
                 ('desk_record.py', '\'fill_price\': ticket.get(\'fill_price\')', '\'uncovered_field\': True, \'fill_price\': ticket.get(\'fill_price\')'),
                 ('signer_delegate_client.py', '\'backoff\', route=route', '\'backoff\', uncovered_field=True, route=route'),
                 ('desk_io.py', 'source + \'_px\': None', 'source + \'_new_field\': None, source + \'_px\': None'),
                 ('desk_anchor_verify.py', '\'anchor_verified\', signal_id', '\'unknown_event\', signal_id')]
    for filename, old, new in mutations:
        assert old in sources[filename]
        try:
            check_writer_fields({**sources, filename: sources[filename].replace(old, new, 1)})
        except AssertionError:
            pass
        else:
            raise AssertionError((filename, 'Uncovered writer accepted'))
    return count
