#!/usr/bin/python
# -*- coding: utf-8 -*-

from collections import OrderedDict
import inspect
from functools import wraps
import epydoc.markup
from six.moves.html_parser import HTMLParser
import tornado.web
from .settings import default_settings, models
from .handlers import swagger_handlers

__author__ = 'serena'


class EpytextParser(HTMLParser):
    a_text = False

    def __init__(self, tag):
        HTMLParser.__init__(self)
        self.tag = tag
        self.data = None

    def handle_starttag(self, tag, attr):
        if tag == self.tag:
            self.a_text = True

    def handle_endtag(self, tag):
        if tag == self.tag:
            self.a_text = False

    def handle_data(self, data):
        if self.a_text:
            self.data = data

    def get_data(self):
        return self.data


class DocParser(object):
    def __init__(self):
        self.notes = None
        self.summary = None
        self.responseClasses = OrderedDict()
        self.responseMessages = []
        self.params = OrderedDict()
        self.properties = OrderedDict()

    def parse_docstring(self, text):
        if text is None:
            return

        errors = []
        doc = epydoc.markup.parse(text, markup='epytext', errors=errors)
        _, fields = doc.split_fields(errors)

        for field in fields:
            tag = field.tag()
            arg = field.arg()
            body = field.body()
            self._get_parser(tag)(arg=arg, body=body)
        return doc

    def _get_parser(self, tag):
        parser = {
            'param': self._parse_param,
            'type': self._parse_type,
            'in': self._parse_in,
            'required': self._parse_required,
            'rtype': self._parse_rtype,
            'property': self._parse_property,
            'ptype': self._parse_ptype,
            'return': self._parse_return,
            'raise': self._parse_return,
            'notes': self._parse_notes,
            'description': self._parse_description,
        }
        return parser.get(tag, self._not_supported)

    def _parse_param(self, **kwargs):
        arg = kwargs.get('arg', None)
        body = self._get_body(**kwargs)
        self.params.setdefault(arg, {}).update({
            'name': arg,
            'description': body,
        })

        if 'paramType' not in self.params[arg]:
            self.params[arg]['paramType'] = 'query'

    def _parse_type(self, **kwargs):
        arg = kwargs.get('arg', None)
        body = self._get_body(**kwargs)
        self.params.setdefault(arg, {}).update({
            'name': arg,
            'dataType': body
        })

    def _parse_in(self, **kwargs):
        arg = kwargs.get('arg', None)
        body = self._get_body(**kwargs)
        self.params.setdefault(arg, {}).update({
            'name': arg,
            'paramType': body
        })

    def _parse_required(self, **kwargs):
        arg = kwargs.get('arg', None)
        body = self._get_body(**kwargs)
        self.params.setdefault(arg, {}).update({
            'name': arg,
            'required': False if body in ['False', 'false'] else True
        })

    def _parse_rtype(self, **kwargs):
        arg = kwargs.get('arg', None)
        body = self._get_body(**kwargs)
        if arg:
            self.responseClasses[arg] = body

    def _parse_property(self, **kwargs):
        arg = kwargs.get('arg', None)
        self.properties.setdefault(arg, {}).update({
            'description': self._get_body(**kwargs)
        })

    def _parse_ptype(self, **kwargs):
        arg = kwargs.get('arg', None)
        code = self._parse_epytext_para('code', **kwargs)
        link = self._parse_epytext_para('link', **kwargs)
        body = self._get_body(**kwargs)
        if code is None:
            self.properties.setdefault(arg, {}).update({
                'type': body
           })
        elif code == 'list':
            self.properties.setdefault(arg, {}).update({
                'type': 'array',
                'items': {'type': link}
            })

    def _parse_return(self, **kwargs):
        arg = kwargs.get('arg', None)
        body = self._get_body(**kwargs)
        response_message = {
            'code': arg,
            'message': body,
        }
        response_class = self.responseClasses.get(arg)
        if response_class:
            response_message['responseModel'] = response_class

        self.responseMessages.append(response_message)

    def _parse_notes(self, **kwargs):
        body = self._get_body(**kwargs)
        self.notes = self._sanitize_doc(body)

    def _parse_description(self, **kwargs):
        body = self._get_body(**kwargs)
        self.summary = self._sanitize_doc(body)

    def _not_supported(self, **kwargs):
        pass

    @staticmethod
    def _sanitize_doc(comment):
        # Epydoc's to_plaintext method will word wrap lines over 75 chars with a newline with no
        # option to override this behavior, sadly. Let's remove those newlines here.
        return comment.replace('\n', ' ') if comment else comment

    @staticmethod
    def _get_body(**kwargs):
        body = kwargs.get('body', None)
        return body.to_plaintext(None).strip() if body else body

    @staticmethod
    def _parse_epytext_para(tag, **kwargs):
        def _parse_epytext(tag, body):
            epytextParser = EpytextParser(tag)
            epytextParser.feed(str(body))
            data = epytextParser.get_data()
            epytextParser.close()
            return data

        body = kwargs.get('body', None)
        return _parse_epytext(tag, body) if body else body


class model(DocParser):
    def __init__(self, *args, **kwargs):
        super(model, self).__init__()
        self.args = args
        self.kwargs = kwargs
        self.required = []
        self.cls = None

    def __call__(self, *args, **kwargs):
        if self.cls:
            return self.cls

        cls = args[0]
        self._parse_model(cls)

        return cls

    def _parse_model(self, cls):
        self.id = cls.__name__
        self.cls = cls
        if '__init__' in dir(cls):
            self._parse_args(cls.__init__)
        self.parse_docstring(inspect.getdoc(cls))
        models.append(self)

    def _parse_args(self, func):
        argspec = inspect.getargspec(func)
        argspec.args.remove("self")
        defaults = {}
        if argspec.defaults:
            defaults = list(zip(argspec.args[-len(argspec.defaults):], argspec.defaults))
        required_args_count = len(argspec.args) - len(defaults)
        for arg in argspec.args[:required_args_count]:
            self.required.append(arg)
            self.properties.setdefault(arg, {'type': 'string'})
        for arg, default in defaults:
            self.properties.setdefault(arg, {'type': 'string', "default": default})


class operation(DocParser):
    def __init__(self, nickname=None, **kwds):
        super(operation, self).__init__()
        self.nickname = nickname
        self.func = None
        self.func_args = []
        self.kwds = kwds

    def __call__(self, *args, **kwds):
        if self.func:
            return self.func(*args, **kwds)

        func = args[0]
        self._parse_operation(func)

        @wraps(func)
        def __wrapper__(*in_args, **in_kwds):
            return self.func(*in_args, **in_kwds)

        __wrapper__.rest_api = self
        return __wrapper__

    def _parse_operation(self, func):
        self.func = func

        self.__name__ = func.__name__
        self._parse_args(func)
        self.parse_docstring(inspect.getdoc(self.func))

    def _parse_args(self, func):
        argspec = inspect.getargspec(func)
        if 'self' in argspec.args:
            argspec.args.remove('self')

        defaults = []
        if argspec.defaults:
            defaults = argspec.args[-len(argspec.defaults):]

        for arg in argspec.args:
            if arg in defaults:
                required = False
            else:
                required = True
            self.params.setdefault(arg, {
                'name': arg,
                'required': required,
                'paramType': 'path',
                'dataType': 'string'
            })
        self.func_args = argspec.args


def docs(**opts):
    default_settings.update(opts)


class Application(tornado.web.Application):
    def __init__(self, handlers=None, default_host="", transforms=None, **settings):
        super(Application, self).__init__(swagger_handlers() + handlers, default_host, transforms, **settings)
        self.handlers = handlers
