"""Append inventory rows to the Google Sheet, growing its table when it fills up."""

import os

from gspread.utils import ValueRenderOption

from utils.sheets_api import get_sheet

TABLE_GROWTH_ROWS = 5
FORMULA_COLUMN_INDEXES = {11, 13, 14}
INVENTORY_WORKSHEET_NAME = "Sales"
INVENTORY_ID_HEADER = "Inventory ID"


def append_row(
    location: str,
    purchase_date,
    item_type: str,
    style_code: str,
    name: str,
    size,
    price_paid,
    sheet=None,
    inventory_id: str | None = None,
):
    try:
        if sheet is None:
            sheet = get_inventory_sheet()

        # Blank cells are the sheet's own formula/derived columns.
        values = [
            str(location),
            str(purchase_date),
            "",
            "Pending",
            "",
            str(item_type),
            str(style_code),
            str(name),
            str(size),
            "",
            float(price_paid),
        ]

        inventory_id_column = None
        if inventory_id:
            inventory_id_column = ensure_inventory_id_column(sheet)
            existing_row = find_inventory_id_row(sheet, inventory_id, inventory_id_column)
            if existing_row is not None:
                return inventory_row_result(
                    location, purchase_date, item_type, style_code, name, size,
                    price_paid, existing_row, inventory_id,
                )

        table = find_inventory_table(sheet, required_columns=len(values))
        if table:
            sheet_row = append_table_row(
                sheet,
                table,
                values,
                inventory_id=inventory_id,
                inventory_id_column=inventory_id_column,
            )
        else:
            sheet_row = append_worksheet_row(
                sheet,
                values,
                inventory_id=inventory_id,
                inventory_id_column=inventory_id_column,
            )
    except Exception as exc:
        raise RuntimeError(f"Failed to append row: {exc}") from exc

    return inventory_row_result(
        location, purchase_date, item_type, style_code, name, size,
        price_paid, sheet_row, inventory_id,
    )


def get_inventory_sheet(worksheet_name: str = INVENTORY_WORKSHEET_NAME):
    sheet_id = os.getenv("SHEET_ID")
    if not sheet_id:
        raise ValueError("SHEET_ID is missing from .env")
    return get_sheet(sheet_id, worksheet_name)


def inventory_row_result(
    location, purchase_date, item_type, style_code, name, size, price_paid, sheet_row,
    inventory_id=None,
) -> dict:
    return {
        "inventory_id": inventory_id,
        "location": location,
        "date_of_purchase": purchase_date,
        "item_type": item_type,
        "style_code": style_code,
        "name": name,
        "size": size,
        "price_paid": price_paid,
        "sheet_row": sheet_row,
    }


def find_inventory_table(sheet, required_columns: int) -> dict | None:
    metadata = sheet.spreadsheet.fetch_sheet_metadata(params={"includeGridData": "false"})
    sheet_metadata = next(
        (
            item
            for item in metadata.get("sheets", [])
            if item.get("properties", {}).get("sheetId") == sheet.id
        ),
        None,
    )
    if not sheet_metadata:
        return None

    for table in sheet_metadata.get("tables", []):
        table_range = table.get("range", {})
        if (
            table_range.get("startRowIndex", 0) == 0
            and table_range.get("startColumnIndex", 0) == 0
            and table_range.get("endColumnIndex", 0) >= required_columns
        ):
            return table
    return None


def append_table_row(
    sheet,
    table: dict,
    values: list,
    inventory_id: str | None = None,
    inventory_id_column: int | None = None,
) -> int:
    table_range = table["range"]
    start_row_index = table_range.get("startRowIndex", 0)
    end_row_index = table_range["endRowIndex"]
    start_column_index = table_range.get("startColumnIndex", 0)
    end_column_index = table_range["endColumnIndex"]
    rows = read_table_rows(
        sheet,
        start_row_index=start_row_index,
        end_row_index=end_row_index,
        start_column_index=start_column_index,
        end_column_index=end_column_index,
    )
    available_rows = available_table_rows(rows, start_row_index)
    target_row = available_rows[0][0] if available_rows else None

    if len(available_rows) <= 1:
        if target_row is None:
            target_row = end_row_index + 1
        new_end_row_index = end_row_index + TABLE_GROWTH_ROWS
        ensure_worksheet_capacity(sheet, new_end_row_index)
        blank_template_row = None
        if available_rows and row_has_formula(available_rows[0][1]):
            blank_template_row = available_rows[0][0]
        template_row = blank_template_row or formula_template_row(rows, start_row_index)
        requests = table_growth_requests(
            sheet_id=sheet.id,
            table=table,
            new_end_row_index=new_end_row_index,
            template_row=template_row,
            blank_template=blank_template_row is not None,
            fill_start_row_index=(target_row - 1),
        )
        sheet.spreadsheet.batch_update({"requests": requests})

    write_inventory_row(
        sheet,
        target_row,
        values,
        inventory_id=inventory_id,
        inventory_id_column=inventory_id_column,
    )
    return target_row


def read_table_rows(
    sheet,
    start_row_index: int,
    end_row_index: int,
    start_column_index: int,
    end_column_index: int,
) -> list[list]:
    range_name = (
        f"{column_letter(start_column_index + 1)}{start_row_index + 1}:"
        f"{column_letter(end_column_index)}{end_row_index}"
    )
    return sheet.get(
        range_name,
        value_render_option=ValueRenderOption.formula,
        maintain_size=True,
        pad_values=True,
    )


def available_table_rows(
    rows: list[list],
    start_row_index: int,
) -> list[tuple[int, list]]:
    # The first row in a Google Sheets table is its header.
    available = []
    for offset, row in enumerate(rows[1:], start=1):
        if not row or not str(row[0]).strip():
            available.append((start_row_index + offset + 1, row))
    return available


def row_has_formula(row: list) -> bool:
    return any(str(value).startswith("=") for value in row)


def formula_template_row(rows: list[list], start_row_index: int) -> int:
    formula_rows = [
        start_row_index + offset + 1
        for offset, row in enumerate(rows[1:], start=1)
        if row_has_formula(row)
    ]
    if not formula_rows:
        raise ValueError("No formula row was found in the inventory table")
    if len(formula_rows) > 1:
        return formula_rows[-2]
    return formula_rows[-1]


def table_growth_requests(
    sheet_id: int,
    table: dict,
    new_end_row_index: int,
    template_row: int,
    blank_template: bool,
    fill_start_row_index: int,
) -> list[dict]:
    table_range = table["range"]
    start_column_index = table_range.get("startColumnIndex", 0)
    end_column_index = table_range["endColumnIndex"]
    old_end_row_index = table_range["endRowIndex"]
    expanded_range = {
        "sheetId": sheet_id,
        "startRowIndex": table_range.get("startRowIndex", 0),
        "endRowIndex": new_end_row_index,
        "startColumnIndex": start_column_index,
        "endColumnIndex": end_column_index,
    }
    template_range = {
        "sheetId": sheet_id,
        "startRowIndex": template_row - 1,
        "endRowIndex": template_row,
        "startColumnIndex": start_column_index,
        "endColumnIndex": end_column_index,
    }
    new_rows_range = {
        "sheetId": sheet_id,
        "startRowIndex": old_end_row_index,
        "endRowIndex": new_end_row_index,
        "startColumnIndex": start_column_index,
        "endColumnIndex": end_column_index,
    }
    fill_rows_range = {
        **new_rows_range,
        "startRowIndex": fill_start_row_index,
    }
    formula_rows_range = {
        **new_rows_range,
        "startRowIndex": template_row,
    }

    requests = [
        {
            "updateTable": {
                "table": {
                    "tableId": str(table["tableId"]),
                    "range": expanded_range,
                },
                "fields": "range",
            }
        },
    ]

    if blank_template:
        requests.extend(
            [
                copy_paste_request(template_range, new_rows_range, "PASTE_NORMAL"),
                copy_paste_request(
                    template_range,
                    new_rows_range,
                    "PASTE_DATA_VALIDATION",
                ),
            ]
        )
    else:
        requests.extend(
            [
                copy_paste_request(template_range, fill_rows_range, "PASTE_FORMAT"),
                copy_paste_request(
                    template_range,
                    fill_rows_range,
                    "PASTE_DATA_VALIDATION",
                ),
                copy_paste_request(
                    template_range,
                    formula_rows_range,
                    "PASTE_FORMULA",
                ),
            ]
        )

    requests.extend(
        clear_non_formula_values_requests(
            sheet_id=sheet_id,
            start_row_index=old_end_row_index,
            end_row_index=new_end_row_index,
            start_column_index=start_column_index,
            end_column_index=end_column_index,
        )
    )
    return requests


def copy_paste_request(source: dict, destination: dict, paste_type: str) -> dict:
    return {
        "copyPaste": {
            "source": source,
            "destination": destination,
            "pasteType": paste_type,
            "pasteOrientation": "NORMAL",
        }
    }


def clear_non_formula_values_requests(
    sheet_id: int,
    start_row_index: int,
    end_row_index: int,
    start_column_index: int,
    end_column_index: int,
) -> list[dict]:
    requests = []
    segment_start = start_column_index
    formula_columns = sorted(
        column
        for column in FORMULA_COLUMN_INDEXES
        if start_column_index <= column < end_column_index
    )

    for formula_column in formula_columns + [end_column_index]:
        if segment_start < formula_column:
            requests.append(
                clear_values_request(
                    sheet_id,
                    start_row_index,
                    end_row_index,
                    segment_start,
                    formula_column,
                )
            )
        segment_start = formula_column + 1
    return requests


def clear_values_request(
    sheet_id: int,
    start_row_index: int,
    end_row_index: int,
    start_column_index: int,
    end_column_index: int,
) -> dict:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row_index,
                "endRowIndex": end_row_index,
                "startColumnIndex": start_column_index,
                "endColumnIndex": end_column_index,
            },
            "cell": {},
            "fields": "userEnteredValue",
        }
    }


def append_worksheet_row(
    sheet,
    values: list,
    inventory_id: str | None = None,
    inventory_id_column: int | None = None,
) -> int:
    next_row = len(sheet.col_values(1)) + 1
    ensure_worksheet_capacity(sheet, next_row)
    write_inventory_row(
        sheet,
        next_row,
        values,
        inventory_id=inventory_id,
        inventory_id_column=inventory_id_column,
    )
    return next_row


def write_inventory_row(
    sheet,
    row_number: int,
    values: list,
    inventory_id: str | None = None,
    inventory_id_column: int | None = None,
) -> None:
    if not inventory_id:
        sheet.update([values], f"A{row_number}")
        return
    if inventory_id_column is None:
        raise ValueError("Inventory ID column is required")

    # A single Sheets values batch keeps the operational cells and permanent ID
    # together without writing across (or replacing) intervening formula cells.
    sheet.batch_update(
        [
            {
                "range": f"A{row_number}:{column_letter(len(values))}{row_number}",
                "values": [values],
            },
            {
                "range": f"{column_letter(inventory_id_column)}{row_number}",
                "values": [[inventory_id]],
            },
        ]
    )


def ensure_inventory_id_column(sheet) -> int:
    headers = sheet_header_values(sheet)
    existing_column = inventory_id_column_from_headers(headers)
    if existing_column is not None:
        return existing_column

    column_number = max(len(headers), inventory_table_width(sheet), 1) + 1
    ensure_worksheet_column_capacity(sheet, column_number)
    sheet.update([[INVENTORY_ID_HEADER]], f"{column_letter(column_number)}1")
    return column_number


def inventory_id_column_from_headers(headers: list) -> int | None:
    matches = [
        index
        for index, value in enumerate(headers, start=1)
        if str(value).strip().lower() == INVENTORY_ID_HEADER.lower()
    ]
    if len(matches) > 1:
        raise ValueError("The Sheet contains more than one Inventory ID column")
    return matches[0] if matches else None


def sheet_header_values(sheet) -> list:
    if hasattr(sheet, "row_values"):
        return list(sheet.row_values(1))
    if hasattr(sheet, "get_all_values"):
        rows = sheet.get_all_values()
    else:
        rows = sheet.get("1:1")
    return list(rows[0]) if rows else []


def inventory_table_width(sheet) -> int:
    try:
        metadata = sheet.spreadsheet.fetch_sheet_metadata(params={"includeGridData": "false"})
    except (AttributeError, KeyError):
        return 0

    widths = []
    for item in metadata.get("sheets", []):
        if item.get("properties", {}).get("sheetId") != sheet.id:
            continue
        for table in item.get("tables", []):
            table_range = table.get("range", {})
            if table_range.get("startRowIndex", 0) == 0:
                widths.append(table_range.get("endColumnIndex", 0))
    return max(widths, default=0)


def find_inventory_id_row(
    sheet,
    inventory_id: str,
    inventory_id_column: int | None = None,
) -> int | None:
    column_number = inventory_id_column or ensure_inventory_id_column(sheet)
    values = sheet.col_values(column_number)
    target = str(inventory_id).strip().upper()
    for row_number, value in enumerate(values, start=1):
        if row_number == 1:
            continue
        if str(value).strip().upper() == target:
            return row_number
    return None


def write_inventory_id(sheet, row_number: int, column_number: int, inventory_id: str) -> None:
    sheet.update(
        [[str(inventory_id).strip().upper()]],
        f"{column_letter(column_number)}{row_number}",
    )


def write_inventory_ids_batch(
    sheet,
    column_number: int,
    assignments: list[tuple[int, str]],
) -> None:
    """Write multiple permanent IDs in one Sheets API request."""

    if not assignments:
        return
    column = column_letter(column_number)
    sheet.batch_update(
        [
            {
                "range": f"{column}{row_number}",
                "values": [[str(inventory_id).strip().upper()]],
            }
            for row_number, inventory_id in assignments
        ]
    )


def ensure_worksheet_capacity(sheet, required_rows: int) -> None:
    if required_rows > sheet.row_count:
        sheet.add_rows(max(TABLE_GROWTH_ROWS, required_rows - sheet.row_count))


def ensure_worksheet_column_capacity(sheet, required_columns: int) -> None:
    current_columns = getattr(sheet, "col_count", required_columns)
    if required_columns > current_columns:
        sheet.add_cols(required_columns - current_columns)


def column_letter(column_number: int) -> str:
    if column_number < 1:
        raise ValueError("column_number must be 1 or greater")
    letters = []
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        letters.append(chr(65 + remainder))
    return "".join(reversed(letters))


def a1_cell(row_number: int, column_number: int) -> str:
    return f"{column_letter(column_number)}{row_number}"


def sheet_value(row: list, one_based_column: int) -> str:
    """Return a cell from a get_all_values() row as stripped text; short rows read blank."""

    index = one_based_column - 1
    value = row[index] if index < len(row) else None
    return "" if value is None else str(value).strip()
