"""Frozen installed Pipe ABI and override copying, isolated from the processor."""

from inspect import ismethod
from types import MappingProxyType
from examples.official_document_workflow.services.exports import DocumentExports
from examples.official_document_workflow.services.ocr import DocumentOCR
from examples.official_document_workflow.services.parsing import DocumentParsing
from examples.official_document_workflow.services.rendering import DocumentRendering
from examples.official_document_workflow.services.sources import DocumentSources
from examples.official_document_workflow.services.transport import ModelTransport
from examples.official_document_workflow.services.values import DocumentValues
from examples.official_document_workflow.services.clarifications import CalendarClarifications


class ServiceOperation:
    """Expose a declared service method; assignments also reach that service.

    This preserves the installed Pipe's operation contract without a generic
    attribute fallback or sharing a processor instance as every service's self.
    """

    def __init__(self, component, service, name):
        self.component, self.service, self.name = component, service, name

    def __get__(self, instance, owner):
        return getattr(
            self.service if instance is None else getattr(instance, self.component),
            self.name,
        )

    def __set__(self, instance, value):
        setattr(getattr(instance, self.component), self.name, value)
        instance.__dict__[self.name] = value

    def __delete__(self, instance):
        delattr(getattr(instance, self.component), self.name)
        instance.__dict__.pop(self.name, None)


def expose_operations(processor, services):
    """Install only the reviewed legacy ABI; service additions stay internal."""
    operations = {}
    from .webui_operations import COMPATIBILITY_OPERATIONS

    for component, names in COMPATIBILITY_OPERATIONS.items():
        service = services[component]
        for name in names:
            if not callable(getattr(service, name, None)):
                raise TypeError(
                    f'Missing document compatibility operation: {component}.{name}'
                )
            if hasattr(processor, name):
                raise TypeError(f'Duplicate document operation: {name}')
            setattr(processor, name, ServiceOperation(component, service, name))
            operations[name] = component
    processor._service_operations = MappingProxyType(operations)


def copy_overrides(original, target):
    overrides = {}
    for name, value in vars(original).items():
        if not callable(value) or not any(
            name in vars(base) for base in type(original).__mro__
        ):
            continue
        if ismethod(value) and value.__self__ is original:
            value = value.__func__.__get__(target, type(target))
        setattr(target, name, value)
        overrides[name] = value
    return overrides


class LegacyPipeOperations:
    def __copy__(self):
        clone = super().__copy__()
        components = {
            'values',
            'rendering',
            'parser',
            'ocr',
            'sources',
            'exports',
            'transport',
            'clarifications',
        }
        for name, value in vars(self).items():
            if (
                name not in components | {'_request'}
                and name not in self._service_operations
            ):
                setattr(clone, name, value)
        for name in components:
            clone.__dict__.update(
                copy_overrides(getattr(self, name), getattr(clone, name))
            )
        return clone


expose_operations(
    LegacyPipeOperations,
    {
        'values': DocumentValues,
        'rendering': DocumentRendering,
        'parser': DocumentParsing,
        'ocr': DocumentOCR,
        'sources': DocumentSources,
        'exports': DocumentExports,
        'transport': ModelTransport,
        'clarifications': CalendarClarifications,
    },
)
