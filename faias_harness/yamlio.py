"""YAML parsing for the harness, SRD-1 and the tools.

`load` parses one YAML document with the safe loader of libyaml (yaml.CSafeLoader) where PyYAML has it, and with the
pure-Python yaml.SafeLoader where it does not. The two loaders build the same objects for each YAML file of the repository. A test
parses each YAML file of the repository with both. libyaml is about 7 times faster here. It ships inside the PyYAML
wheels, so it adds no dependency.
"""
import yaml


def loader():
    """The safe loader to use: libyaml's where PyYAML has it, else the pure-Python one."""
    return getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def load(text):
    """The object of the one YAML document in `text` (str or bytes). It is the object that the pure-Python safe
    loader gives."""
    return yaml.load(text, Loader=loader())
