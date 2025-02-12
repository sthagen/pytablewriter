"""
.. codeauthor:: Tsuyoshi Hombashi <tsuyoshi.hombashi@gmail.com>
"""

import abc
import copy
import math
import warnings
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Optional, Union, cast

import typepy
from dataproperty import (
    Align,
    ColumnDataProperty,
    DataProperty,
    DataPropertyExtractor,
    Format,
    MatrixFormatting,
    Preprocessor,
)
from dataproperty.typing import TransFunc
from tabledata import TableData, convert_idx_to_alphabet, to_value_matrix
from typepy import Typecode, extract_typepy_from_dtype

from .._logger import WriterLogger
from ..error import EmptyTableDataError, EmptyTableNameError, EmptyValueError, NotSupportedError
from ..style import (
    Cell,
    CheckStyleFilterKeywordArgsFunc,
    ColSeparatorStyleFilterFunc,
    Style,
    StyleFilterFunc,
    StylerInterface,
    ThousandSeparator,
    fetch_theme,
)
from ..typehint import Integer, TypeHint
from ._common import HEADER_ROW
from ._interface import TableWriterInterface
from ._msgfy import to_error_message


if TYPE_CHECKING:
    import pandas
    import tablib

    from .._table_format import TableFormat

_ts_to_flag: dict[ThousandSeparator, int] = {
    ThousandSeparator.NONE: Format.NONE,
    ThousandSeparator.COMMA: Format.THOUSAND_SEPARATOR,
    ThousandSeparator.SPACE: Format.THOUSAND_SEPARATOR,
    ThousandSeparator.UNDERSCORE: Format.THOUSAND_SEPARATOR,
}


def header_style_filter(cell: Cell, **kwargs: Any) -> Optional[Style]:
    if cell.is_header_row():
        return Style(align=Align.CENTER)

    return None


DEFAULT_STYLE_FILTERS: list[StyleFilterFunc] = [header_style_filter]


class AbstractTableWriter(TableWriterInterface, metaclass=abc.ABCMeta):
    """
    An abstract base class of table writer classes.

    Args:
        max_precision (int): Maximum decimal places for real number values.

        dequote (bool): If |True|, dequote values in :py:attr:`~.value_matrix`.

    .. py:attribute:: stream

        Stream to write tables.
        You can use arbitrary streams which support ``write``methods
        such as ``sys.stdout``, file stream, ``StringIO``, and so forth.
        Defaults to ``sys.stdout``.

        :Example:
            :ref:`example-configure-stream`

    .. py:attribute:: is_write_header
        :type: bool

        Write headers of a table if the value is |True|.

    .. py:attribute:: is_padding
        :type: bool

        Padding for each item in the table if the value is |True|.

    .. py:attribute:: iteration_length
        :type: int

        The number of iterations to write a table.
        This value is used in :py:meth:`.write_table_iter` method.
        (defaults to ``-1``, which means the number of iterations is indefinite)

    .. py:attribute:: style_filter_kwargs
        :type: Dict[str, Any]

        Extra keyword arguments for style filter functions.
        These arguments will pass to filter functions added by
        :py:meth:`.add_style_filter` or :py:meth:`.add_col_separator_style_filter`

    .. py:attribute:: colorize_terminal
        :type: bool
        :value: True

        [Only available for text format writers] [experimental]
        If |True|, colorize text outputs with |Style|.

    .. py:attribute:: enable_ansi_escape
        :type: bool
        :value: True

        [Only available for text format writers]
        If |True|, applies ANSI escape sequences to the terminal's text outputs with |Style|.

    .. py:attribute:: write_callback

        The value expected to a function.
        The function is called when each of the iterations of writing a table
        completed. (defaults to |None|)
        For example, a callback function definition is as follows:

        .. code:: python

            def callback_example(iter_count: int, iter_length: int) -> None:
                print("{:d}/{:d}".format(iter_count, iter_length))

        Arguments that passed to the callback are:

        - first argument: current iteration number (start from ``1``)
        - second argument: a total number of iteration
    """

    FORMAT_NAME = "abstract"

    @property
    def margin(self) -> int:
        raise NotImplementedError()

    @margin.setter
    def margin(self, value: int) -> None:
        raise NotImplementedError()

    @property
    def value_matrix(self) -> Sequence:
        """Data of a table to be outputted."""

        return self.__value_matrix_org

    @value_matrix.setter
    def value_matrix(self, value_matrix: Sequence) -> None:
        self.__set_value_matrix(value_matrix)
        self.__clear_preprocess()

    @property
    def table_format(self) -> "TableFormat":
        """TableFormat: Get the format of the writer."""

        from .._table_format import TableFormat

        table_format = TableFormat.from_name(self.format_name)
        assert table_format

        return table_format

    @property
    def stream(self) -> Any:
        return self._stream

    @stream.setter
    def stream(self, value: Any) -> None:
        self._stream = value

    @abc.abstractmethod
    def _write_table(self, **kwargs: Any) -> None:
        pass

    def __init__(self, **kwargs: Any) -> None:
        self._logger = WriterLogger(self)

        self.table_name = kwargs.get("table_name", "")
        self.value_matrix = kwargs.get("value_matrix", [])

        self.is_write_header = kwargs.get("is_write_header", True)
        self.is_write_header_separator_row = kwargs.get("is_write_header_separator_row", True)
        self.is_write_value_separator_row = kwargs.get("is_write_value_separator_row", False)
        self.is_write_opening_row = kwargs.get("is_write_opening_row", False)
        self.is_write_closing_row = kwargs.get("is_write_closing_row", False)

        self._use_default_header = False

        self._dp_extractor = DataPropertyExtractor(max_precision=kwargs.get("max_precision"))
        self._dp_extractor.min_column_width = 1
        self._dp_extractor.strip_str_header = '"'
        self._dp_extractor.preprocessor = Preprocessor(dequote=kwargs.get("dequote", True))
        self._dp_extractor.set_type_value(Typecode.NONE, "")
        self._dp_extractor.matrix_formatting = MatrixFormatting.HEADER_ALIGNED
        self._dp_extractor.update_strict_level_map({Typecode.BOOL: 1})

        self.is_formatting_float = kwargs.get("is_formatting_float", True)
        self.is_padding = kwargs.get("is_padding", True)

        self.headers = kwargs.get("headers", [])
        self.type_hints = kwargs.get("type_hints", [])
        self._quoting_flags = {
            Typecode.BOOL: False,
            Typecode.DATETIME: True,
            Typecode.DICTIONARY: False,
            Typecode.INFINITY: False,
            Typecode.INTEGER: False,
            Typecode.IP_ADDRESS: True,
            Typecode.LIST: False,
            Typecode.NAN: False,
            Typecode.NONE: False,
            Typecode.NULL_STRING: True,
            Typecode.REAL_NUMBER: False,
            Typecode.STRING: True,
        }

        self._is_require_table_name = False
        self._is_require_header = False

        self.iteration_length: int = kwargs.get("iteration_length", -1)
        self.write_callback = kwargs.get(
            "write_callback",
            lambda _iter_count, _iter_length: None,  # defaults to NOP callback
        )
        self._iter_count: Optional[int] = None

        self.__default_style: Style
        self.default_style = kwargs.get("default_style", Style())

        self.__col_style_list: list[Optional[Style]] = []
        self.column_styles = kwargs.get("column_styles", [])

        self._style_filters: list[StyleFilterFunc] = copy.deepcopy(DEFAULT_STYLE_FILTERS)
        self._enable_style_filter = True
        self._styler = self._create_styler(self)
        self.style_filter_kwargs: dict[str, Any] = kwargs.get("style_filter_kwargs", {})
        self._check_style_filter_kwargs_funcs: list[CheckStyleFilterKeywordArgsFunc] = []
        self.__colorize_terminal = kwargs.get("colorize_terminal", True)
        self.__enable_ansi_escape = kwargs.get("enable_ansi_escape", True)

        self.max_workers = kwargs.get("max_workers", 1)

        if "dataframe" in kwargs:
            self.from_dataframe(kwargs["dataframe"])

        self.__clear_preprocess()

    def _repr_html_(self) -> str:
        from .text._html import HtmlTableWriter

        writer = HtmlTableWriter(
            table_name=self.table_name,
            headers=self.headers,
            value_matrix=self.value_matrix,
            column_styles=self.column_styles,
            colorize_terminal=self.colorize_terminal,
            enable_ansi_escape=self.enable_ansi_escape,
        )
        writer._dp_extractor = self._dp_extractor

        return writer.dumps()

    @property
    def value_preprocessor(self) -> Preprocessor:
        return self._dp_extractor.preprocessor

    def __clear_preprocess_status(self) -> None:
        self._is_complete_table_dp_preprocess = False
        self._is_complete_table_property_preprocess = False
        self._is_complete_header_preprocess = False
        self._is_complete_value_matrix_preprocess = False

    def __clear_preprocess_data(self) -> None:
        self._column_dp_list: list[ColumnDataProperty] = []
        self._table_headers: list[str] = []
        self._table_value_matrix: list[Union[list[str], dict]] = []
        self._table_value_dp_matrix: Sequence[Sequence[DataProperty]] = []

    @property
    def headers(self) -> Sequence[str]:
        """Sequence[str]: Headers of a table to be outputted."""

        return self._dp_extractor.headers

    @headers.setter
    def headers(self, value: Sequence[str]) -> None:
        self._dp_extractor.headers = value

    @property
    def is_formatting_float(self) -> bool:
        return self._dp_extractor.is_formatting_float

    @is_formatting_float.setter
    def is_formatting_float(self, value: bool) -> None:
        if self._dp_extractor.is_formatting_float == value:
            return

        self._dp_extractor.is_formatting_float = value
        self.__clear_preprocess()

    @property
    def max_workers(self) -> int:
        return self._dp_extractor.max_workers

    @max_workers.setter
    def max_workers(self, value: int) -> None:
        self._dp_extractor.max_workers = value

    @property
    def tabledata(self) -> TableData:
        """tabledata.TableData: Get tabular data of the writer."""

        return TableData(
            self.table_name,
            self.headers,
            self.value_matrix,
            max_workers=self.max_workers,
            max_precision=self._dp_extractor.max_precision,
        )

    @property
    def table_name(self) -> str:
        """str: Name of a table."""

        return self._table_name

    @table_name.setter
    def table_name(self, value: str) -> None:
        self._table_name = value

    @property
    def type_hints(self) -> list[TypeHint]:
        """
        Type hints for each column of the tabular data.
        Writers convert data for each column using the type hints information
        before writing tables when you call ``write_xxx`` methods.

        Acceptable values are as follows:

            - |None| (automatically detect column type from values in the column)
            - :py:class:`pytablewriter.typehint.Bool` or ``"bool"``
            - :py:class:`pytablewriter.typehint.DateTime` or ``"datetime"``
            - :py:class:`pytablewriter.typehint.Dictionary` or ``"dict"``
            - :py:class:`pytablewriter.typehint.Infinity` or ``"inf"``
            - :py:class:`pytablewriter.typehint.Integer` or ``"int"``
            - :py:class:`pytablewriter.typehint.IpAddress` or ``"ipaddr"``
            - :py:class:`pytablewriter.typehint.List` or ``"list"``
            - :py:class:`pytablewriter.typehint.Nan` or ``"nan"``
            - :py:class:`pytablewriter.typehint.NoneType` or ``"none"``
            - :py:class:`pytablewriter.typehint.NullString` or ``"nullstr"``
            - :py:class:`pytablewriter.typehint.RealNumber` or ``"realnumber"`` or ``"float"``
            - :py:class:`pytablewriter.typehint.String` or ``"str"``

        If a type-hint value is not |None|, the writer tries to
        convert data for each data in a column to type-hint class.
        If the type-hint value is |None| or failed to convert data,
        the writer automatically detects column data type from
        the column data.

        If ``type_hints`` is |None|, the writer automatically detects data types
        for all of the columns and writes a table using detected column types.

        Defaults to |None|.

        Examples:
            - :ref:`example-type-hint-js`
            - :ref:`example-type-hint-python`
        """

        return self._dp_extractor.column_type_hints

    @type_hints.setter
    def type_hints(self, value: Sequence[Union[str, TypeHint]]) -> None:
        hints = list(value)
        if self.type_hints == hints:
            return

        self.__set_type_hints(hints)
        self.__clear_preprocess()

    @property
    def default_style(self) -> Style:
        """Style: Default |Style| of table cells."""

        return self.__default_style

    @default_style.setter
    def default_style(self, style: Optional[Style]) -> None:
        if style is None:
            style = Style()

        if not isinstance(style, Style):
            raise TypeError("default_style must be a Style instance")

        try:
            if self.__default_style == style:
                return
        except AttributeError:
            # not yet initialized
            pass

        self.__default_style = style
        self._dp_extractor.default_format_flags = _ts_to_flag[
            self.__default_style.thousand_separator
        ]
        self.__clear_preprocess()

    @property
    def column_styles(self) -> list[Optional[Style]]:
        """List[Optional[Style]]: |Style| for each column."""

        return self.__col_style_list

    @column_styles.setter
    def column_styles(self, value: Sequence[Optional[Style]]) -> None:
        if self.__col_style_list == value:
            return

        self.__col_style_list = list(value)

        if self.__col_style_list:
            self._dp_extractor.format_flags_list = [
                _ts_to_flag[self._get_col_style(col_idx).thousand_separator]
                for col_idx in range(len(self.__col_style_list))
            ]
        else:
            self._dp_extractor.format_flags_list = []

        self.__clear_preprocess()

    @property
    def colorize_terminal(self) -> bool:
        return self.__colorize_terminal

    @colorize_terminal.setter
    def colorize_terminal(self, value: bool) -> None:
        if self.__colorize_terminal == value:
            return

        self.__colorize_terminal = value
        self.__clear_preprocess()

    @property
    def enable_ansi_escape(self) -> bool:
        return self.__enable_ansi_escape

    @enable_ansi_escape.setter
    def enable_ansi_escape(self, value: bool) -> None:
        if self.__enable_ansi_escape == value:
            return

        self.__enable_ansi_escape = value
        self.__clear_preprocess()

    @property
    def _quoting_flags(self) -> dict[Typecode, bool]:
        return self._dp_extractor.quoting_flags

    @_quoting_flags.setter
    def _quoting_flags(self, value: dict[Typecode, bool]) -> None:
        self._dp_extractor.quoting_flags = value
        self.__clear_preprocess()

    def add_style_filter(self, style_filter: StyleFilterFunc) -> None:
        """Add a style filter function to the writer.

        Args:
            style_filter:
                A function called for each table cell to apply a style to table cells.
                The function will be required to implement the following Protocol:

                .. code-block:: python

                    class StyleFilterFunc(Protocol):
                        def __call__(self, cell: Cell, **kwargs: Any) -> Optional[Style]:
                            ...

                If more than one style filter function is added to the writer,
                it will be called from the last one added.
                These style functions should return |None| when not needed to apply styles.
                If all of the style functions returned |None|,
                :py:attr:`~.default_style` will be used.

                You can pass keyword arguments to style filter functions via
                :py:attr:`~.style_filter_kwargs`. In default, the attribute includes:

                    - ``writer``: the writer instance that the caller of a ``style_filter function``
        """

        self._style_filters.insert(0, style_filter)
        self.__clear_preprocess()

    def add_col_separator_style_filter(self, style_filter: ColSeparatorStyleFilterFunc) -> None:
        self._logger.logger.debug(
            "add_col_separator_style_filter method is only implemented in text format writer classes"
        )

    def clear_theme(self) -> None:
        """Remove all of the style filters."""

        if not self._style_filters:
            return

        self._style_filters = copy.deepcopy(DEFAULT_STYLE_FILTERS)
        self._check_style_filter_kwargs_funcs = []
        self.__clear_preprocess()

    def enable_style_filter(self) -> None:
        """Enable style filters."""

        if self._enable_style_filter is True:
            return

        self._enable_style_filter = True
        self.__clear_preprocess()

    def disable_style_filter(self, clear_filters: bool = False) -> None:
        """Disable style filters.

        Args:
            clear_filters (bool):
                If |True|, clear all of the style filters.
                Defaults to |False|.
        """

        if clear_filters:
            self.clear_theme()
            return

        if self._enable_style_filter is False:
            return

        self._enable_style_filter = False
        self.__clear_preprocess()

    def set_style(self, column: Union[str, int], style: Style) -> None:
        """Set |Style| for a specific column.

        Args:
            column (|int| or |str|):
                Column specifier. Column index or header name correlated with the column.
            style (|Style|):
                Style value to be set to the column.

        Raises:
            ValueError: Raised when the column specifier is invalid.
        """

        column_idx = None

        while len(self.headers) > len(self.__col_style_list):
            self.__col_style_list.append(None)

        if isinstance(column, int):
            column_idx = column
        elif isinstance(column, str):
            try:
                column_idx = self.headers.index(column)
            except ValueError:
                pass

        if column_idx is not None:
            self.__col_style_list[column_idx] = style
            self.__clear_preprocess()
            self._dp_extractor.format_flags_list = [
                _ts_to_flag[self._get_col_style(col_idx).thousand_separator]
                for col_idx in range(len(self.__col_style_list))
            ]
            return

        raise ValueError(f"column must be an int or string: actual={column}")

    def set_theme(self, theme: str, **kwargs: Any) -> None:
        """Set style filters for a theme.

        Args:
            theme (str):
                Name of the theme. pytablewriter theme plugin must be installed
                corresponding to the theme name.

        Raises:
            RuntimeError: Raised when a theme plugin does not install.
        """

        try:
            fetched_theme = fetch_theme(theme.strip())
        except RuntimeError as e:
            warnings.warn(f"{e}", UserWarning)
            return

        if fetched_theme.style_filter:
            self.add_style_filter(fetched_theme.style_filter)

        if fetched_theme.col_separator_style_filter:
            self.add_col_separator_style_filter(fetched_theme.col_separator_style_filter)

        if fetched_theme.check_style_filter_kwargs:
            self._check_style_filter_kwargs_funcs.append(fetched_theme.check_style_filter_kwargs)

        self.style_filter_kwargs.update(**kwargs)

    def __is_skip_close(self) -> bool:
        try:
            from _pytest.capture import EncodedFile

            if isinstance(self.stream, EncodedFile):
                # avoid closing streams for pytest
                return True
        except ImportError:
            pass

        try:
            from _pytest.capture import CaptureIO

            if isinstance(self.stream, CaptureIO):
                # avoid closing streams for pytest
                return True
        except ImportError:
            try:
                # for pytest 5.4.1 or older versions
                from _pytest.compat import CaptureIO  # type: ignore

                if isinstance(self.stream, CaptureIO):
                    # avoid closing streams for pytest
                    return True
            except ImportError:
                pass

        try:
            from ipykernel.iostream import OutStream

            if isinstance(self.stream, OutStream):
                # avoid closing streams for Jupyter Notebook
                return True
        except ImportError:
            pass

        return False

    def close(self) -> None:
        """
        Close the current |stream|.
        """

        if self.stream is None:
            return

        try:
            self.stream.isatty()

            if self.stream.name in ["<stdin>", "<stdout>", "<stderr>"]:
                return
        except AttributeError:
            pass
        except ValueError:
            # raised when executing an operation to a closed stream
            pass

        if self.__is_skip_close():
            return

        try:
            self.stream.close()
        except AttributeError:
            self._logger.logger.warning(
                f"the stream has no close method implementation: type={type(self.stream)}"
            )
        finally:
            self._stream = None

    def from_tabledata(self, value: TableData, is_overwrite_table_name: bool = True) -> None:
        """
        Set tabular attributes to the writer from |TableData|.
        The following attributes are configured:

        - :py:attr:`~.table_name`.
        - :py:attr:`~.headers`.
        - :py:attr:`~.value_matrix`.

        |TableData| can be created from various data formats by
        ``pytablereader``. More detailed information can be found in
        https://pytablereader.rtfd.io/en/latest/

        :param tabledata.TableData value: Input table data.
        """

        self.__clear_preprocess()

        if is_overwrite_table_name:
            self.table_name = value.table_name if value.table_name else ""

        self.headers = value.headers
        self.value_matrix = list(value.rows)

        if not value.has_value_dp_matrix:
            return

        self._table_value_dp_matrix = value.value_dp_matrix
        self._column_dp_list = self._dp_extractor.to_column_dp_list(
            self._table_value_dp_matrix, self._column_dp_list
        )
        self.__set_type_hints([col_dp.type_class for col_dp in self._column_dp_list])

        self._is_complete_table_dp_preprocess = True

    def from_csv(self, csv_source: str, delimiter: str = ",") -> None:
        """
        Set tabular attributes to the writer from a character-separated values (CSV) data source.
        The following attributes are set to the writer by the method:

        - :py:attr:`~.headers`.
        - :py:attr:`~.value_matrix`.

        :py:attr:`~.table_name` also be set if the CSV data source is a file.
        In that case, :py:attr:`~.table_name` is as same as the filename.

        Args:
            csv_source (str):
                Input CSV data source can be designated CSV text or a CSV file path.

            delimiter (str):
                Delimiter character of the CSV data source.
                Defaults to ``,``.

        Examples:
            :ref:`example-from-csv`

        :Dependency Packages:
            - `pytablereader <https://github.com/thombashi/pytablereader>`__
        """

        import pytablereader as ptr

        loader = ptr.CsvTableTextLoader(csv_source, quoting_flags=self._quoting_flags)
        loader.delimiter = delimiter
        try:
            for table_data in loader.load():
                self.from_tabledata(table_data, is_overwrite_table_name=False)
            return
        except ptr.DataError:
            pass

        loader = ptr.CsvTableFileLoader(csv_source, quoting_flags=self._quoting_flags)
        loader.delimiter = delimiter
        for table_data in loader.load():
            self.from_tabledata(table_data)

    def from_dataframe(
        self,
        dataframe: "pandas.DataFrame",
        add_index_column: bool = False,
        overwrite_type_hints: bool = True,
    ) -> None:
        """
        Set tabular attributes to the writer from :py:class:`pandas.DataFrame`.
        The following attributes are set by the method:

            - :py:attr:`~.headers`
            - :py:attr:`~.value_matrix`
            - :py:attr:`~.type_hints`

        Args:
            dataframe(pandas.DataFrame or |str|):
                Input pandas.DataFrame object or path to a DataFrame pickle.
            add_index_column(bool, optional):
                If |True|, add a column of ``index`` of the ``dataframe``.
                Defaults to |False|.
            overwrite_type_hints(bool):
                If |True|, Overwrite type hints with dtypes within the DataFrame.

        Example:
            :ref:`example-from-pandas-dataframe`
        """

        if isinstance(dataframe, str):
            import pandas as pd

            df = pd.read_pickle(dataframe)
        else:
            df = dataframe

        self.headers = list(df.columns.values)

        if not self.type_hints or overwrite_type_hints:
            self.type_hints = [extract_typepy_from_dtype(dtype) for dtype in df.dtypes]  # type: ignore

        if add_index_column:
            index_header = str(df.index.name) if df.index.name else " "
            self.headers = [index_header] + self.headers
            if self.type_hints:
                self.type_hints = [Integer] + self.type_hints
            self.value_matrix = [
                [index] + list(row) for index, row in zip(df.index.tolist(), df.values.tolist())
            ]
        else:
            self.value_matrix = df.values.tolist()

    def from_series(self, series: "pandas.Series", add_index_column: bool = True) -> None:
        """
        Set tabular attributes to the writer from :py:class:`pandas.Series`.
        The following attributes are set by the method:

            - :py:attr:`~.headers`
            - :py:attr:`~.value_matrix`
            - :py:attr:`~.type_hints`

        Args:
            series(pandas.Series):
                Input pandas.Series object.
            add_index_column(bool, optional):
                If |True|, add a column of ``index`` of the ``series``.
                Defaults to |True|.
        """

        if series.name:
            self.headers = [str(series.name)]
        else:
            self.headers = ["value"]

        self.type_hints = [extract_typepy_from_dtype(series.dtype)]

        if add_index_column:
            self.headers = [""] + self.headers
            if self.type_hints:
                self.type_hints = [None] + self.type_hints
            self.value_matrix = [
                [index] + [value] for index, value in zip(series.index.tolist(), series.tolist())
            ]
        else:
            self.value_matrix = [[value] for value in series.tolist()]

    def from_tablib(self, tablib_dataset: "tablib.Dataset") -> None:
        """
        Set tabular attributes to the writer from :py:class:`tablib.Dataset`.
        """

        if tablib_dataset.headers:
            self.headers = tablib_dataset.headers
        self.value_matrix = [row for row in tablib_dataset]

    def from_writer(
        self, writer: "AbstractTableWriter", is_overwrite_table_name: bool = True
    ) -> None:
        """
        Copy attributes from another table writer class instance.

        Args:
            writer (pytablewriter.writer.AbstractTableWriter):
                Another table writer instance.
            is_overwrite_table_name (bool, optional):
                Overwrite the table name of the writer with the table name of the ``writer``.
                Defaults to |True|.
        """

        self.__clear_preprocess()

        if is_overwrite_table_name:
            self.table_name = str(writer.table_name)

        self.headers = writer.headers
        self.value_matrix = writer.value_matrix

        self.type_hints = writer.type_hints
        self.column_styles = writer.column_styles
        self._style_filters = writer._style_filters
        self.style_filter_kwargs = writer.style_filter_kwargs
        self.margin = writer.margin

        self._table_headers = writer._table_headers
        self._table_value_dp_matrix = writer._table_value_dp_matrix
        self._column_dp_list = writer._column_dp_list
        self._table_value_matrix = writer._table_value_matrix

        self.stream = writer.stream

        self._is_complete_table_dp_preprocess = writer._is_complete_table_dp_preprocess
        self._is_complete_table_property_preprocess = writer._is_complete_table_property_preprocess
        self._is_complete_header_preprocess = writer._is_complete_header_preprocess
        self._is_complete_value_matrix_preprocess = writer._is_complete_value_matrix_preprocess

    def register_trans_func(self, trans_func: TransFunc) -> None:
        self._dp_extractor.register_trans_func(trans_func)
        self.__clear_preprocess()

    def update_preprocessor(self, **kwargs: Any) -> None:
        # TODO: documentation
        #   is_escape_formula_injection: for CSV/Excel

        if not self._dp_extractor.update_preprocessor(**kwargs):
            return

        self.__clear_preprocess()

    def write_table(self, **kwargs: Any) -> None:
        """
        |write_table|.
        """

        with self._logger:
            try:
                self._verify_property()
            except EmptyTableDataError:
                self._logger.logger.debug("no tabular data found")
                return

            self._write_table(**kwargs)

    def _write_table_iter(self, **kwargs: Any) -> None:
        if not self.support_split_write:
            raise NotSupportedError("the class not supported the write_table_iter method")

        self._verify_style_filter_kwargs()
        self._verify_table_name()
        self._verify_stream()

        if all(
            [typepy.is_empty_sequence(self.headers), typepy.is_empty_sequence(self.value_matrix)]
        ):
            self._logger.logger.debug("no tabular data found")
            return

        self._verify_header()

        self._logger.logger.debug(f"_write_table_iter: iteration-length={self.iteration_length:d}")

        stash_is_write_header = self.is_write_header
        stach_is_write_opening_row = self.is_write_opening_row
        stash_is_write_closing_row = self.is_write_closing_row

        try:
            self.is_write_closing_row = False
            self._iter_count = 1

            for work_matrix in self.value_matrix:
                is_final_iter = all(
                    [self.iteration_length > 0, self._iter_count >= self.iteration_length]
                )

                if is_final_iter:
                    self.is_write_closing_row = True

                self.__set_value_matrix(work_matrix)
                self.__clear_preprocess_status()

                with self._logger:
                    self._write_table(**kwargs)

                    if not is_final_iter:
                        self._write_value_row_separator()

                self.is_write_opening_row = False
                self.is_write_header = False

                self.write_callback(self._iter_count, self.iteration_length)

                # update typehint for the next iteration
                """
                if self.type_hints is None:
                    self.__set_type_hints([
                        column_dp.type_class for column_dp in self._column_dp_list
                    ])
                """

                if is_final_iter:
                    break

                self._iter_count += 1
        finally:
            self.is_write_header = stash_is_write_header
            self.is_write_opening_row = stach_is_write_opening_row
            self.is_write_closing_row = stash_is_write_closing_row
            self._iter_count = None

    def _get_padding_len(
        self, column_dp: ColumnDataProperty, value_dp: Optional[DataProperty] = None
    ) -> int:
        if not self.is_padding:
            return 0

        try:
            return cast(DataProperty, value_dp).get_padding_len(column_dp.ascii_char_width)
        except AttributeError:
            return column_dp.ascii_char_width

    def _to_header_item(self, col_dp: ColumnDataProperty, value_dp: DataProperty) -> str:
        style = self._fetch_style(HEADER_ROW, col_dp, value_dp)
        header = self._apply_style_to_header_item(col_dp, value_dp, style)
        header = self._styler.apply_terminal_style(header, style=style)

        return header

    def _apply_style_to_header_item(
        self, col_dp: ColumnDataProperty, value_dp: DataProperty, style: Style
    ) -> str:
        return self._styler.apply_align(
            self._styler.apply(col_dp.dp_to_str(value_dp), style=style), style=style
        )

    def _to_row_item(self, row_idx: int, col_dp: ColumnDataProperty, value_dp: DataProperty) -> str:
        style = self._fetch_style(row_idx, col_dp, value_dp)
        value = self._apply_style_to_row_item(row_idx, col_dp, value_dp, style)

        return self._styler.apply_terminal_style(value, style=style)

    def _apply_style_to_row_item(
        self, row_idx: int, col_dp: ColumnDataProperty, value_dp: DataProperty, style: Style
    ) -> str:
        return self._styler.apply_align(
            self._styler.apply(col_dp.dp_to_str(value_dp), style=style), style=style
        )

    def _fetch_style_from_filter(
        self, row_idx: int, col_dp: ColumnDataProperty, value_dp: DataProperty, default_style: Style
    ) -> Style:
        if not self._enable_style_filter:
            return default_style

        self.style_filter_kwargs.update({"writer": self})

        style: Optional[Style] = None
        for style_filter in self._style_filters:
            style = style_filter(
                Cell(
                    row=row_idx,
                    col=col_dp.column_index,
                    value=value_dp.data,
                    default_style=default_style,
                ),
                **self.style_filter_kwargs,
            )
            if style:
                break

        if style is None:
            style = copy.deepcopy(default_style)

        if style.align is None or (style.align == Align.AUTO and row_idx >= 0):
            style.align = self.__retrieve_align_from_data(col_dp, value_dp)

        if style.padding is None:
            style.padding = self._get_padding_len(col_dp, value_dp)

        return style

    def _get_col_style(self, col_idx: int) -> Style:
        try:
            style = self.column_styles[col_idx]
        except (TypeError, IndexError, KeyError):
            pass
        else:
            if style:
                return style

        return self.default_style

    def _get_align(self, col_idx: int, default_align: Align) -> Align:
        align = self._get_col_style(col_idx).align

        if align is None:
            return default_align

        if align == Align.AUTO:
            return default_align

        return align

    def __retrieve_align_from_data(
        self, col_dp: ColumnDataProperty, value_dp: DataProperty
    ) -> Align:
        if col_dp.typecode == Typecode.STRING and (
            value_dp.typecode in (Typecode.INTEGER, Typecode.REAL_NUMBER)
            or value_dp.typecode == Typecode.STRING
            and value_dp.is_include_ansi_escape
        ):
            return value_dp.align

        return col_dp.align

    def _verify_property(self) -> None:
        self._verify_style_filter_kwargs()
        self._verify_table_name()
        self._verify_stream()

        if all(
            [
                typepy.is_empty_sequence(self.headers),
                typepy.is_empty_sequence(self.value_matrix),
                typepy.is_empty_sequence(self._table_value_dp_matrix),
            ]
        ):
            raise EmptyTableDataError()

        self._verify_header()
        try:
            self._verify_value_matrix()
        except EmptyValueError:
            pass

    def __set_value_matrix(self, value_matrix: Sequence) -> None:
        self.__value_matrix_org = value_matrix

    def __set_type_hints(self, type_hints: Sequence[Union[str, TypeHint]]) -> None:
        self._dp_extractor.column_type_hints = type_hints

    def _verify_style_filter_kwargs(self) -> None:
        for checker in self._check_style_filter_kwargs_funcs:
            checker(**self.style_filter_kwargs)

    def _verify_table_name(self) -> None:
        if all([self._is_require_table_name, typepy.is_null_string(self.table_name)]):
            raise EmptyTableNameError(
                "table_name must be a string, with at least one or more character."
            )

    def _verify_stream(self) -> None:
        if self.stream is None:
            raise OSError("null output stream")

    def _verify_header(self) -> None:
        if self._is_require_header and not self._use_default_header:
            self._validate_empty_header()

    def _validate_empty_header(self) -> None:
        """
        Raises:
            ValueError: If the |headers| is empty.
        """

        if typepy.is_empty_sequence(self.headers):
            raise ValueError("headers expected to have one or more header names")

    def _verify_value_matrix(self) -> None:
        if typepy.is_empty_sequence(self.value_matrix):
            raise EmptyValueError()

    def _create_styler(self, writer: "AbstractTableWriter") -> StylerInterface:
        from ..style._styler import NullStyler

        return NullStyler(writer)

    def _preprocess_table_dp(self) -> None:
        if self._is_complete_table_dp_preprocess:
            return

        self._logger.logger.debug("_preprocess_table_dp")

        if typepy.is_empty_sequence(self.headers) and self._use_default_header:
            self.headers = [
                convert_idx_to_alphabet(col_idx)
                for col_idx in range(len(self.__value_matrix_org[0]))
            ]

        try:
            self._table_value_dp_matrix = self._dp_extractor.to_dp_matrix(
                to_value_matrix(self.headers, self.__value_matrix_org)
            )
        except TypeError as e:
            self._logger.logger.debug(to_error_message(e))
            self._table_value_dp_matrix = []

        self._column_dp_list = self._dp_extractor.to_column_dp_list(
            self._table_value_dp_matrix, self._column_dp_list
        )

        self._is_complete_table_dp_preprocess = True

    def _fetch_style(self, row: int, col_dp: ColumnDataProperty, value_dp: DataProperty) -> Style:
        default_style = self._get_col_style(col_dp.column_index)
        return self._fetch_style_from_filter(row, col_dp, value_dp, default_style)

    def _preprocess_table_property(self) -> None:
        if self._is_complete_table_property_preprocess:
            return

        self._logger.logger.debug("_preprocess_table_property")

        if self._iter_count == 1:
            for column_dp in self._column_dp_list:
                column_dp.extend_width(int(math.ceil(column_dp.ascii_char_width * 0.25)))

        header_dp_list = self._dp_extractor.to_header_dp_list()
        if not header_dp_list:
            return

        for column_dp in self._column_dp_list:
            style = self._get_col_style(column_dp.column_index)
            header_style = self._fetch_style(
                HEADER_ROW, column_dp, header_dp_list[column_dp.column_index]
            )
            body_width = self._styler.get_additional_char_width(style)
            header_width = self._styler.get_additional_char_width(header_style)
            column_dp.extend_body_width(max(body_width, header_width))

        self._is_complete_table_property_preprocess = True

    def _preprocess_header(self) -> None:
        if self._is_complete_header_preprocess:
            return

        self._logger.logger.debug("_preprocess_header")

        self._table_headers = [
            self._to_header_item(col_dp, header_dp)
            for col_dp, header_dp in zip(
                self._column_dp_list, self._dp_extractor.to_header_dp_list()
            )
        ]

        self._is_complete_header_preprocess = True

    def _preprocess_value_matrix(self) -> None:
        if self._is_complete_value_matrix_preprocess:
            return

        self._logger.logger.debug(
            f"_preprocess_value_matrix: value-rows={len(self._table_value_dp_matrix)}"
        )

        self._table_value_matrix = [
            [
                self._to_row_item(row_idx, col_dp, value_dp)
                for col_dp, value_dp in zip(self._column_dp_list, value_dp_list)
            ]
            for row_idx, value_dp_list in enumerate(self._table_value_dp_matrix)
        ]

        self._is_complete_value_matrix_preprocess = True

    def _preprocess(self) -> None:
        self._preprocess_table_dp()
        self._preprocess_table_property()
        self._preprocess_header()
        self._preprocess_value_matrix()

    def _clear_preprocess(self) -> None:
        self.__clear_preprocess()

    def __clear_preprocess(self) -> None:
        self.__clear_preprocess_status()
        self.__clear_preprocess_data()
