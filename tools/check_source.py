"""Validate the separate V2/V3 indexes and installable source directories."""
import ast
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def check(root):
    assert json.loads((root / 'package.json').read_text(encoding='utf-8')) == {}
    for generation in ('v2', 'v3'):
        check_generation(root, generation)
    ET.parse(root / 'icons/thundersubtitles.svg')
    v2, v3 = [root / ('plugins.' + v) / 'thundersubtitles' for v in ('v2', 'v3')]
    for filename in ('media.py', 'name_search.py'):
        assert (v2 / filename).read_bytes() == (v3 / filename).read_bytes(), filename + ' must stay in sync'
    legacy = (v2 / 'thunder.py').read_text(encoding='utf-8')
    legacy = legacy.replace('try:\n    from .name_search import NameSearchError, SearchCancelled, combined_search\nexcept ImportError:\n    # The core can also be loaded independently by development tools.\n    from name_search import NameSearchError, SearchCancelled, combined_search', 'from .name_search import NameSearchError, SearchCancelled, combined_search')
    assert legacy == (v3 / 'thunder.py').read_text(encoding='utf-8'), 'V2/V3 subtitle core must stay in sync'
    print('V2/V3 source layout, metadata, version history and Python syntax verified.')


def check_generation(root, generation):
    """Check one index without allowing it to resolve another generation's code."""
    index = json.loads((root / ('package.' + generation + '.json')).read_text(encoding='utf-8'))
    for plugin_id, metadata in index.items():
        directory = root / ('plugins.' + generation) / plugin_id.lower()
        module = ast.parse((directory / '__init__.py').read_text(encoding='utf-8'))
        cls = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == plugin_id)
        values = {node.targets[0].id: ast.literal_eval(node.value) for node in cls.body
                  if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant)}
        assert values['plugin_version'] == metadata['version']
        assert values['plugin_name'] == metadata['name']
        assert values['plugin_desc'] == metadata['description']
        assert metadata['release'] is False
        history = list(metadata['history'])
        assert history[0] == 'v' + metadata['version']
        assert history == sorted(history, key=lambda v: tuple(int(x) for x in v.lstrip('v').split('.')), reverse=True)
        if generation == 'v2':
            assert metadata['v3'] is False
        else:
            assert metadata['system_version'] == '>=3.2.3'
            old = json.loads((root / 'package.v2.json').read_text(encoding='utf-8'))[plugin_id]
            assert int(metadata['version'].split('.')[0]) > int(old['version'].split('.')[0])
            assert not list(directory.rglob('test_*.py'))
        for source in directory.rglob('*.py'):
            text = source.read_text(encoding='utf-8')
            compile(text, str(source), 'exec')
            if generation == 'v3':
                tree = ast.parse(text)
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        module = node.module or ''
                        assert not module.startswith(('app.core', 'app.helper', 'app.utils', 'app.db.models', 'app.sdk._legacy'))


if __name__ == '__main__': check(Path(__file__).resolve().parents[1])
