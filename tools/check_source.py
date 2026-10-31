"""Validate the V2 market index and its installable source directory."""
import ast
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def check(root):
    assert json.loads((root / 'package.json').read_text(encoding='utf-8')) == {}
    index = json.loads((root / 'package.v2.json').read_text(encoding='utf-8'))
    for plugin_id, metadata in index.items():
        directory = root / 'plugins.v2' / plugin_id.lower()
        module = ast.parse((directory / '__init__.py').read_text(encoding='utf-8'))
        cls = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == plugin_id)
        values = {node.targets[0].id: ast.literal_eval(node.value) for node in cls.body
                  if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant)}
        assert values['plugin_version'] == metadata['version']
        assert values['plugin_name'] == metadata['name']
        assert values['plugin_desc'] == metadata['description']
        assert metadata['release'] is False
        assert metadata['v3'] is False
        assert 'v' + metadata['version'] in metadata['history']
        for source in directory.rglob('*.py'):
            compile(source.read_text(encoding='utf-8'), str(source), 'exec')
    ET.parse(root / 'icons/thundersubtitles.svg')
    print('V2 source layout, metadata and Python syntax verified.')


if __name__ == '__main__': check(Path(__file__).resolve().parents[1])
