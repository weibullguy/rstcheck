#!/usr/bin/env python3
# pylint: disable=too-many-lines

# Copyright (C) 2013-2022 Steven Myint
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""Checks code blocks in reStructuredText."""
import argparse
import configparser
import contextlib
import copy
import doctest
import io
import json
import locale
import multiprocessing
import os
import pathlib
import re
import shlex
import shutil
import subprocess  # noqa: S404
import sys
import tempfile
import typing
import xml.etree.ElementTree  # noqa: S405

import docutils.core
import docutils.io
import docutils.nodes
import docutils.parsers.rst
import docutils.utils
import docutils.writers
import typing_extensions


try:
    import tomli

    TOMLI_INSTALLED = True
except ImportError:
    TOMLI_INSTALLED = False

try:
    import sphinx

    SPHINX_INSTALLED = sphinx.version_info >= (2, 0)
except (AttributeError, ImportError):
    SPHINX_INSTALLED = False

if SPHINX_INSTALLED:
    import sphinx.application
    import sphinx.directives
    import sphinx.domains.c
    import sphinx.domains.cpp
    import sphinx.domains.javascript
    import sphinx.domains.python
    import sphinx.domains.std
    import sphinx.roles


__version__ = "5.0.0"


if SPHINX_INSTALLED:
    SPHINX_CODE_BLOCK_DELTA = -1

RSTCHECK_COMMENT_RE = re.compile(r"\.\. rstcheck:")


# This is for the cases where code in a readme uses includes in that directory.
INCLUDE_FLAGS = ["-I.", "-I.."]
CONFIG_FILES = [".rstcheck.cfg", "setup.cfg"]
if TOMLI_INSTALLED:
    CONFIG_FILES = [".rstcheck.cfg", "pyproject.toml", "setup.cfg"]


class IgnoreDict(typing_extensions.TypedDict, total=False):
    """Type for the ignore dictionary passed around some functions."""

    languages: typing.List[typing.Optional[str]]
    messages: str
    directives: typing.List[str]


ErrorTuple = typing.Tuple[int, str]
"""Tuple with line number and error message."""

YieldedErrorTuple = typing.Generator[ErrorTuple, None, None]
"""Yielded version of type `ErrorTuple`."""

CheckerRunFunction = typing.Callable[..., YieldedErrorTuple]
"""Function to run checks.

Returned by closure of type `CheckerFunction`.
"""

CheckerFunction = typing.Callable[[str, str], CheckerRunFunction]
"""Closure to return check runner function."""

RunCheckFunction = typing.Callable[..., YieldedErrorTuple]
"""Wrapper function for `CheckerRunFunction` functions.

Returned by a closure.
"""


class Error(Exception):
    """rstcheck exception."""

    def __init__(self, message: str, line_number: int) -> None:
        """Init of custom rstcheck exception."""
        self.line_number = line_number
        Exception.__init__(self, message)


class CodeBlockDirective(docutils.parsers.rst.Directive):
    """Code block directive."""

    has_content = True
    optional_arguments = 1

    def run(self) -> typing.List[docutils.nodes.literal_block]:
        """Run directive."""
        try:
            language = self.arguments[0]
        except IndexError:
            language = ""
        code = "\n".join(self.content)
        literal = docutils.nodes.literal_block(code, code)
        literal["classes"].append("code-block")
        literal["language"] = language
        return [literal]


def register_code_directive(
    ignore_code_directive: bool = False,
    ignore_codeblock_directive: bool = False,
    ignore_sourcecode_directive: bool = False,
) -> None:
    """Register code directive."""
    if not SPHINX_INSTALLED:
        if not ignore_code_directive:
            docutils.parsers.rst.directives.register_directive("code", CodeBlockDirective)
        # NOTE: docutils maps `code-block` and `sourcecode` to `code`
        if not ignore_codeblock_directive:
            docutils.parsers.rst.directives.register_directive("code-block", CodeBlockDirective)
        if not ignore_sourcecode_directive:
            docutils.parsers.rst.directives.register_directive("sourcecode", CodeBlockDirective)


def strip_byte_order_mark(text: str) -> str:
    """Return text with byte order mark (BOM) removed."""
    try:
        return text.encode("utf-8").decode("utf-8-sig")
    except UnicodeError:
        return text


def check(  # noqa: CCR001
    source: str,
    filename: str = "<string>",
    report_level: int = docutils.utils.Reporter.INFO_LEVEL,
    ignore: typing.Optional[IgnoreDict] = None,
    debug: bool = False,
) -> YieldedErrorTuple:
    """Yield errors.

    Use lower report_level for noisier error output.

    Each yielded error is a tuple of the form:

        (line_number, message)

    Line numbers are indexed at 1 and are with respect to the full RST file.

    Each code block is checked asynchronously in a subprocess.

    Note that this function mutates state by calling the ``docutils``
    ``register_*()`` functions.

    """
    ignore = ignore or {}

    # Do this at call time rather than import time to avoid unnecessarily
    # mutating state.
    register_code_directive(
        "code" in ignore.get("directives", []),
        "code-block" in ignore.get("directives", []),
        "sourcecode" in ignore.get("directives", []),
    )
    load_ignore_sphinx()

    try:
        ignore.setdefault("languages", []).extend(find_ignored_languages(source))
    except Error as error:
        yield (error.line_number, f"{error}")

    writer = CheckWriter(source, filename, ignore=ignore)

    string_io = io.StringIO()

    # This is a hack to avoid false positive from docutils (#23). docutils
    # mistakes BOMs for actual visible letters. This results in the "underline
    # too short" warning firing.
    source = strip_byte_order_mark(source)

    try:
        docutils.core.publish_string(
            source,
            writer=writer,
            source_path=filename,
            settings_overrides={
                "halt_level": 5,
                "report_level": report_level,
                "warning_stream": string_io,
            },
        )
    except docutils.utils.SystemMessage:
        pass
    except AttributeError:
        # Sphinx will sometimes throw an exception trying to access
        # "self.state.document.settings.env". Ignore this for now until we
        # figure out a better approach.
        if debug:
            raise

    for checker in writer.checkers:
        yield from checker()

    rst_errors = string_io.getvalue().strip()
    if rst_errors:
        for message in rst_errors.splitlines():
            try:
                ignore_regex = ignore.get("messages", "")
                if ignore_regex and re.search(ignore_regex, message):
                    continue
                yield parse_gcc_style_error_message(message, filename=filename, has_column=False)
            except ValueError:
                continue


def find_ignored_languages(source: str) -> typing.Generator[str, None, None]:  # noqa: CCR001
    """Yield ignored languages.

    Languages are ignored via comment.

    For example, to ignore C++, JSON, and Python:

    >>> list(find_ignored_languages('''
    ... Example
    ... =======
    ...
    ... .. rstcheck: ignore-language=cpp,json
    ...
    ... .. rstcheck: ignore-language=python
    ... '''))
    ['cpp', 'json', 'python']

    """
    for (index, line) in enumerate(source.splitlines()):
        match = RSTCHECK_COMMENT_RE.match(line)
        if match:
            key_and_value = line[match.end() :].strip().split("=")
            if len(key_and_value) != 2:
                raise Error('Expected "key=value" syntax', line_number=index + 1)

            if key_and_value[0] == "ignore-language":
                for language in key_and_value[1].split(","):
                    yield language.strip()


def _check_file(
    parameters: typing.Tuple[str, argparse.Namespace]
) -> typing.Tuple[str, typing.List[ErrorTuple]]:
    """Return list of errors."""
    (filename, args) = parameters

    if filename == "-":
        input_file_contents = sys.stdin.read()
    else:
        with contextlib.closing(docutils.io.FileInput(source_path=filename)) as input_file:
            input_file_contents = input_file.read()

    args = load_configuration_from_file(os.path.dirname(os.path.realpath(filename)), args)

    ignore_directives_and_roles(args.ignore_directives, args.ignore_roles)

    for substitution in args.ignore_substitutions:
        input_file_contents = input_file_contents.replace(f"|{substitution}|", f"x{substitution}x")

    ignore: IgnoreDict = {
        "languages": args.ignore_language,
        "messages": args.ignore_messages,
        "directives": args.ignore_directives,
    }
    all_errors = []
    for error in check(
        input_file_contents,
        filename=filename,
        report_level=args.report,
        ignore=ignore,
        debug=args.debug,
    ):
        all_errors.append(error)
    return (filename, all_errors)


def check_python(code: str) -> YieldedErrorTuple:
    """Yield errors."""
    try:
        compile(code, "<string>", "exec")
    except SyntaxError as exception:
        yield (int(exception.lineno or 0), exception.msg)


def check_json(code: str) -> YieldedErrorTuple:
    """Yield errors."""
    try:
        json.loads(code)
    except ValueError as exception:
        message = f"{exception}"
        line_number = 0

        found = re.search(r": line\s+([0-9]+)[^:]*$", message)
        if found:
            line_number = int(found.group(1))

        yield (int(line_number), message)


def check_xml(code: str) -> YieldedErrorTuple:
    """Yield errors."""
    try:
        xml.etree.ElementTree.fromstring(code)  # noqa: S314
    except xml.etree.ElementTree.ParseError as exception:
        message = f"{exception}"
        line_number = 0

        found = re.search(r": line\s+([0-9]+)[^:]*$", message)
        if found:
            line_number = int(found.group(1))

        yield (int(line_number), message)


def check_rst(code: str, ignore: IgnoreDict) -> YieldedErrorTuple:
    """Yield errors in nested RST code."""
    filename = "<string>"

    yield from check(code, filename=filename, ignore=ignore)


def check_doctest(code: str) -> YieldedErrorTuple:
    """Yield doctest syntax errors.

    This does not run the test as that would be unsafe. Nor does this
    check the Python syntax in the doctest. That could be purposely
    incorrect for testing purposes.

    """
    parser = doctest.DocTestParser()
    try:
        parser.parse(code)
    except ValueError as exception:
        message = f"{exception}"
        match = re.match("line ([0-9]+)", message)
        if match:
            yield (int(match.group(1)), message)


def split_comma_separated(text: str) -> typing.List[str]:
    """Return list of split and stripped strings."""
    return [t.strip() for t in text.split(",") if t.strip()]


def _get_directives_and_roles_from_sphinx() -> typing.Tuple[typing.List[str], typing.List[str]]:
    """Return a tuple of Sphinx directive and roles loaded from sphinx."""
    sphinx_directives = list(sphinx.domains.std.StandardDomain.directives)
    sphinx_roles = list(sphinx.domains.std.StandardDomain.roles)

    for domain in [
        sphinx.domains.c.CDomain,
        sphinx.domains.cpp.CPPDomain,
        sphinx.domains.javascript.JavaScriptDomain,
        sphinx.domains.python.PythonDomain,
    ]:

        sphinx_directives += list(domain.directives) + [
            f"{domain.name}:{item}" for item in list(domain.directives)
        ]

        sphinx_roles += list(domain.roles) + [
            f"{domain.name}:{item}" for item in list(domain.roles)
        ]

    sphinx_directives += list(
        sphinx.application.docutils.directives._directives  # pylint: disable=protected-access
    )
    sphinx_roles += list(
        sphinx.application.docutils.roles._roles  # pylint: disable=protected-access
    )
    if "code" in sphinx_directives:
        sphinx_directives.remove("code")
    if "code-block" in sphinx_directives:
        sphinx_directives.remove("code-block")
    if "include" in sphinx_directives:
        sphinx_directives.remove("include")

    return (sphinx_directives, sphinx_roles)


class IgnoredDirective(docutils.parsers.rst.Directive):
    """Stub for unknown directives."""

    has_content = True

    def run(self) -> typing.List:
        """Do nothing."""
        return []


def _ignore_role(
    name: str,
    rawtext: str,
    text: str,
    lineno: int,
    inliner: docutils.parsers.rst.states.Inliner,
    options: typing.Optional[typing.Dict[str, typing.Any]] = None,
    content: typing.Optional[typing.List[str]] = None,
) -> typing.Tuple[typing.List, typing.List]:
    """Stub for unknown roles."""
    # pylint: disable=unused-argument,too-many-arguments
    return ([], [])


def load_ignore_sphinx() -> None:
    """Register Sphinx directives and roles to ignore."""
    if not SPHINX_INSTALLED:
        return

    (directives, roles) = _get_directives_and_roles_from_sphinx()

    ignore_directives_and_roles(directives, roles)


def find_config(  # noqa: CCR001
    directory_or_file: str, debug: bool = False
) -> typing.Optional[str]:
    """Return configuration filename.

    If `directory_or_file` is a file, return the real-path of that file. If it
    is a directory, find the configuration (any file name in CONFIG_FILES) in
    that directory or its ancestors.
    """
    directory_or_file = os.path.realpath(directory_or_file)
    if os.path.isfile(directory_or_file):
        if debug:
            print(f"using config file {directory_or_file}", file=sys.stderr)
        return directory_or_file
    directory = directory_or_file

    while directory:
        for filename in CONFIG_FILES:
            candidate = os.path.join(directory, filename)
            if os.path.exists(candidate):
                if debug:
                    print(f"using config file {candidate}", file=sys.stderr)
                return candidate

        parent_directory = os.path.dirname(directory)
        if parent_directory == directory:
            break
        directory = parent_directory

    return None


def load_configuration_from_file(directory: str, args: argparse.Namespace) -> argparse.Namespace:
    """Return new ``args`` with configuration loaded from file."""
    args = copy.copy(args)

    directory_or_file = directory
    if args.config is not None:
        directory_or_file = args.config

    options = _get_options(directory_or_file, debug=args.debug)

    args.report = args.report or options.get("report", "info")
    threshold_dictionary = docutils.frontend.OptionParser.thresholds
    args.report = int(threshold_dictionary.get(args.report, args.report))

    args.ignore_messages = args.ignore_messages or options.get("ignore_messages", "")

    args.ignore_language = split_comma_separated(
        args.ignore_language or options.get("ignore_language", "")
    )

    args.ignore_directives = split_comma_separated(
        args.ignore_directives or options.get("ignore_directives", "")
    )

    args.ignore_substitutions = split_comma_separated(
        args.ignore_substitutions or options.get("ignore_substitutions", "")
    )

    args.ignore_roles = split_comma_separated(args.ignore_roles or options.get("ignore_roles", ""))

    return args


def _get_options(directory_or_file: str, debug: bool = False) -> typing.Dict[str, str]:
    config_path = find_config(directory_or_file, debug=debug)
    if not config_path:
        return {}

    if pathlib.Path(config_path).name == "pyproject.toml":
        return _get_pyproject_options(config_path)

    parser = configparser.ConfigParser()
    parser.read(config_path)
    try:
        return dict(parser.items("rstcheck"))
    except configparser.NoSectionError:
        return {}


RstcheckTOMLConfig = typing.Dict[str, typing.Union[str, typing.List[str]]]


def _get_pyproject_options(config_path: str) -> typing.Dict[str, str]:

    with open(config_path, "rb") as conf_file:
        config = tomli.load(conf_file)

    options_from_file: typing.Optional[RstcheckTOMLConfig] = config.get("tool", {}).get(
        "rstcheck", None
    )

    if options_from_file is None:
        return {}

    options = {}

    # tomli returns a list of strings and ConfigParser returns a comma
    # separated string.  This makes the options from pyproject.toml consistent
    # with the options read from other configuration files.  The try block
    # accounts for pyproject files without a rstcheck section.
    for option in [
        "ignore_directives",
        "ignore_roles",
        "ignore_messages",
        "ignore_language",
        "report",
    ]:
        option_value = options_from_file.get(option, None)
        if option_value is None:
            continue

        if isinstance(option_value, list):
            option_value = ",".join(option_value)

        options[option] = option_value

    return options


def ignore_directives_and_roles(directives: typing.List[str], roles: typing.List[str]) -> None:
    """Ignore directives/roles in docutils."""
    for directive in directives:
        docutils.parsers.rst.directives.register_directive(directive, IgnoredDirective)

    for role in roles:
        docutils.parsers.rst.roles.register_local_role(role, _ignore_role)


# The checker functions below return a checker. This is for purposes of
# asynchronous checking. As we visit each code block, a subprocess gets
# launched to run the checker. They all run in the background until we finish
# traversing the document. At that point, we accumulate the errors.


def bash_checker(code: str, working_directory: str) -> CheckerRunFunction:
    """Return checker."""
    run = run_in_subprocess(code, ".bash", ["bash", "-n"], working_directory=working_directory)

    def run_check() -> YieldedErrorTuple:
        """Yield errors."""
        result = run()
        if result:
            (output, filename) = result
            prefix = filename + ": line "
            for line in output.splitlines():
                if not line.startswith(prefix):
                    continue
                message = line[len(prefix) :]
                split_message = message.split(":", 1)
                yield (int(split_message[0]) - 1, split_message[1].strip())

    return run_check


def c_checker(code: str, working_directory: str) -> CheckerRunFunction:
    """Return checker."""
    return gcc_checker(
        code,
        ".c",
        [os.getenv("CC", "gcc")]
        + shlex.split(os.getenv("CFLAGS", ""))
        + shlex.split(os.getenv("CPPFLAGS", ""))
        + INCLUDE_FLAGS,
        working_directory=working_directory,
    )


def cpp_checker(code: str, working_directory: str) -> CheckerRunFunction:
    """Return checker."""
    return gcc_checker(
        code,
        ".cpp",
        [os.getenv("CXX", "g++")]
        + shlex.split(os.getenv("CXXFLAGS", ""))
        + shlex.split(os.getenv("CPPFLAGS", ""))
        + INCLUDE_FLAGS,
        working_directory=working_directory,
    )


def gcc_checker(
    code: str, filename_suffix: str, arguments: typing.List[str], working_directory: str
) -> CheckerRunFunction:
    """Return checker."""
    run = run_in_subprocess(
        code,
        filename_suffix,
        arguments + ["-pedantic", "-fsyntax-only"],
        working_directory=working_directory,
    )

    def run_check() -> YieldedErrorTuple:
        """Yield errors."""
        result = run()
        if result:
            (output, filename) = result
            for line in output.splitlines():
                try:
                    yield parse_gcc_style_error_message(line, filename=filename)
                except ValueError:
                    continue

    return run_check


def parse_gcc_style_error_message(
    message: str, filename: str, has_column: typing.Optional[bool] = True
) -> ErrorTuple:
    """Parse GCC-style error message.

    Return (line_number, message). Raise ValueError if message cannot be
    parsed.
    """
    colons = 2 if has_column else 1
    prefix = filename + ":"
    if not message.startswith(prefix):
        raise ValueError("Message cannot be parsed.")
    message = message[len(prefix) :]
    split_message = message.split(":", colons)
    line_number = int(split_message[0])
    return (line_number, split_message[colons].strip())


def get_encoding() -> str:
    """Return preferred encoding."""
    return locale.getpreferredencoding() or sys.getdefaultencoding()


def run_in_subprocess(
    code: str,
    filename_suffix: typing.AnyStr,
    arguments: typing.List[str],
    working_directory: typing.AnyStr,
) -> typing.Callable[..., typing.Optional[typing.Tuple[str, str]]]:
    """Return None on success."""
    temporary_file = tempfile.NamedTemporaryFile(  # pylint: disable=consider-using-with
        mode="wb", suffix=filename_suffix
    )
    temporary_file.write(code.encode("utf-8"))
    temporary_file.flush()

    process = subprocess.Popen(  # pylint: disable=consider-using-with  # noqa: S603
        arguments + [temporary_file.name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=working_directory,
    )

    def run() -> typing.Optional[typing.Tuple[str, str]]:
        """Yield errors."""
        raw_result = process.communicate()
        if process.returncode != 0:
            return (raw_result[1].decode(get_encoding()), temporary_file.name)
        return None

    return run


class CheckTranslator(docutils.nodes.NodeVisitor):
    """Visits code blocks and checks for syntax errors in code."""

    def __init__(
        self,
        document: docutils.nodes.document,
        file_contents: str,
        filename: str,
        ignore: typing.Optional[IgnoreDict],
    ) -> None:
        """Init CheckTranslator."""
        docutils.nodes.NodeVisitor.__init__(self, document)
        self.checkers: typing.List[RunCheckFunction] = []
        self.file_contents = file_contents
        self.filename = filename
        self.working_directory = os.path.dirname(os.path.realpath(filename))
        self.ignore = ignore or {}
        self.ignore.setdefault("languages", []).append(None)

    def visit_doctest_block(self, node: docutils.nodes.Element) -> None:
        """Check syntax of doctest."""
        if "doctest" in self.ignore["languages"]:
            return

        self._add_check(
            node=node,
            run=lambda: check_doctest(node.rawsource),
            language="doctest",
            is_code_node=False,
        )

    def visit_literal_block(self, node: docutils.nodes.Element) -> None:
        """Check syntax of code block."""
        # For "..code-block:: language"
        language = node.get("language", None)
        is_code_node = False
        if not language:
            # For "..code:: language"
            is_code_node = True
            classes = node.get("classes")
            if "code" in classes:
                language = classes[-1]
            else:
                return

        if language in self.ignore["languages"]:
            return

        if language == "doctest" or (
            language == "python" and node.rawsource.lstrip().startswith(">>> ")
        ):
            self.visit_doctest_block(node)
            raise docutils.nodes.SkipNode

        check_dict: typing.Dict[str, CheckerFunction] = {
            "bash": bash_checker,
            "c": c_checker,
            "cpp": cpp_checker,
            "json": lambda source, _: lambda: check_json(source),
            "xml": lambda source, _: lambda: check_xml(source),
            "python": lambda source, _: lambda: check_python(source),
            "rst": lambda source, _: lambda: check_rst(source, ignore=self.ignore),
        }
        checker = check_dict.get(language)

        if checker:
            run = checker(node.rawsource, self.working_directory)
            self._add_check(node=node, run=run, language=language, is_code_node=is_code_node)

        raise docutils.nodes.SkipNode

    def visit_paragraph(self, node: docutils.nodes.Element) -> None:
        """Check syntax of reStructuredText."""
        find = re.search(r"\[[^\]]+\]\([^\)]+\)", node.rawsource)
        if find is not None:
            self.document.reporter.warning(
                "(rst) Link is formatted in Markdown style.", base_node=node
            )

    def _add_check(  # noqa: CCR001
        self,
        node: docutils.nodes.Element,
        run: CheckerRunFunction,
        language: str,
        is_code_node: bool,
    ) -> None:
        """Add checker that will be run."""

        def run_check() -> YieldedErrorTuple:  # noqa: CCR001
            """Yield errors."""
            all_results = run()
            if all_results is not None:
                if all_results:
                    for result in all_results:
                        error_offset = result[0] - 1

                        line_number = getattr(node, "line", None)
                        if line_number is not None:
                            yield (
                                beginning_of_code_block(
                                    node=node,
                                    line_number=line_number,
                                    full_contents=self.file_contents,
                                    is_code_node=is_code_node,
                                )
                                + error_offset,
                                f"({language}) {result[1]}",
                            )
                else:
                    yield (0, "unknown error")

        self.checkers.append(run_check)

    def unknown_visit(self, node: docutils.nodes.Node) -> None:
        """Ignore."""

    def unknown_departure(self, node: docutils.nodes.Node) -> None:
        """Ignore."""


def beginning_of_code_block(
    node: docutils.nodes.Element, line_number: int, full_contents: str, is_code_node: bool
) -> int:
    """Return line number of beginning of code block."""
    if SPHINX_INSTALLED and not is_code_node:
        delta = len(node.non_default_attributes())
        current_line_contents = full_contents.splitlines()[line_number:]
        blank_lines = next((i for (i, x) in enumerate(current_line_contents) if x), 0)
        return line_number + delta - 1 + blank_lines - 1 + SPHINX_CODE_BLOCK_DELTA

    lines = full_contents.splitlines()
    code_block_length = len(node.rawsource.splitlines())

    with contextlib.suppress(IndexError):
        # Case where there are no extra spaces.
        if lines[line_number - 1].strip():
            return line_number - code_block_length + 1

    # The offsets are wrong if the RST text has multiple blank lines after
    # the code block. This is a workaround.
    for line_no in range(line_number, 1, -1):
        if lines[line_no - 2].strip():
            break

    return line_no - code_block_length


class CheckWriter(docutils.writers.Writer):
    """Runs CheckTranslator on code blocks."""

    def __init__(self, file_contents: str, filename: str, ignore: IgnoreDict) -> None:
        """Init CheckWriter."""
        docutils.writers.Writer.__init__(self)
        self.checkers: typing.List[RunCheckFunction] = []
        self.file_contents = file_contents
        self.filename = filename
        self.ignore = ignore

    def translate(self) -> None:
        """Run CheckTranslator."""
        visitor = CheckTranslator(
            self.document,
            file_contents=self.file_contents,
            filename=self.filename,
            ignore=self.ignore,
        )
        self.document.walkabout(visitor)
        self.checkers += visitor.checkers


def decode_filename(filename: typing.Union[str, bytes]) -> str:
    """Return Unicode filename."""
    if isinstance(filename, bytes):
        return filename.decode(sys.getfilesystemencoding())
    return filename


def parse_args() -> argparse.Namespace:
    """Return parsed command-line arguments."""
    threshold_choices = docutils.frontend.OptionParser.threshold_choices

    parser = argparse.ArgumentParser(
        description=__doc__ + (" Sphinx is enabled." if SPHINX_INSTALLED else ""), prog="rstcheck"
    )

    parser.add_argument("files", nargs="+", type=decode_filename, help="files to check")
    parser.add_argument("--config", metavar="CONFIG", default=None, help="location of config file")
    parser.add_argument(
        "-r", "--recursive", action="store_true", help="run recursively over directories"
    )
    parser.add_argument(
        "--report",
        metavar="level",
        choices=threshold_choices,
        help="report system messages at or higher than "
        "level; "
        + ", ".join(choice for choice in threshold_choices if not choice.isdigit())
        + " (default: %(default)s)",
    )
    parser.add_argument(
        "--ignore-language",
        "--ignore",
        metavar="language",
        default="",
        help="comma-separated list of languages to ignore",
    )
    parser.add_argument(
        "--ignore-messages",
        metavar="messages",
        default="",
        help="python regex that match the messages to ignore",
    )
    parser.add_argument(
        "--ignore-directives",
        metavar="directives",
        default="",
        help="comma-separated list of directives to ignore",
    )
    parser.add_argument(
        "--ignore-substitutions",
        metavar="substitutions",
        default="",
        help="comma-separated list of substitutions to ignore",
    )
    parser.add_argument(
        "--ignore-roles",
        metavar="roles",
        default="",
        help="comma-separated list of roles to ignore",
    )
    parser.add_argument("--debug", action="store_true", help="show messages helpful for debugging")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)

    args = parser.parse_args()

    if "-" in args.files:
        if len(args.files) > 1:
            parser.error("'-' for standard in can only be checked alone")
    else:
        args.files = list(find_files(filenames=args.files, recursive=args.recursive))

    return args


def output_message(
    text: typing.Union[typing.AnyStr, Exception], output_file: typing.TextIO = sys.stderr
) -> None:
    """Output message to terminal."""
    if output_file.encoding is None:
        # If the output file does not support Unicode, encode it to a byte
        # string. On some machines, this occurs when Python is redirecting to
        # file (or piping to something like Vim).
        text = text.encode("utf-8")

    print(text, file=output_file)


@contextlib.contextmanager
def enable_sphinx_if_possible() -> typing.Generator[None, None, None]:
    """Register Sphinx directives and roles."""
    if SPHINX_INSTALLED:
        srcdir = tempfile.mkdtemp()
        outdir = os.path.join(srcdir, "_build")
        try:
            sphinx.application.Sphinx(
                srcdir=srcdir,
                confdir=None,
                outdir=outdir,
                doctreedir=outdir,
                buildername="dummy",
                status=None,  # type: ignore[arg-type] # NOTE: type hint is incorrect
            )
            yield
        finally:
            shutil.rmtree(srcdir)
    else:
        yield


def match_file(filename: str) -> bool:
    """Return True if file is okay for modifying/recursing."""
    base_name = os.path.basename(filename)

    if base_name.startswith("."):
        return False

    if not os.path.isdir(filename) and not filename.lower().endswith(".rst"):
        return False

    return True


def find_files(filenames: typing.List[str], recursive: bool) -> typing.Generator[str, None, None]:
    """Yield filenames."""
    while filenames:
        name = filenames.pop(0)
        if recursive and os.path.isdir(name):
            for root, directories, children in os.walk(name):
                filenames += [
                    os.path.join(root, f) for f in children if match_file(os.path.join(root, f))
                ]
                directories[:] = [d for d in directories if match_file(os.path.join(root, d))]
        else:
            yield name


def main() -> int:  # noqa: CCR001
    """Return 0 on success."""
    args = parse_args()

    if not args.files:
        return 0

    with enable_sphinx_if_possible():
        status = 0
        pool_size = multiprocessing.cpu_count()
        if sys.platform == "win32":
            # Work around https://bugs.python.org/issue45077
            pool_size = min(pool_size, 61)
        with multiprocessing.Pool(pool_size) as pool:
            try:
                if len(args.files) > 1:
                    results = pool.map(_check_file, [(name, args) for name in args.files])
                else:
                    # This is for the case where we read from standard in.
                    results = [_check_file((args.files[0], args))]

                for (filename, errors) in results:
                    for error in errors:
                        line_number = error[0]
                        message = error[1]

                        if not re.match(r"\([A-Z]+/[0-9]+\)", message):
                            message = "(ERROR/3) " + message

                        output_message(f"{filename}:{line_number}: {message}")

                        status = 1
            except (OSError, UnicodeError) as exception:
                output_message(exception)
                status = 1

        return status


if __name__ == "__main__":
    sys.exit(main())
