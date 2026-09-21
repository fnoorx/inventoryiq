from services.sheets import append_row


class FakeSpreadsheet:
    def __init__(self, sheet_id=123, tables=None):
        self.sheet_id = sheet_id
        self.tables = tables or []
        self.batch_updates = []

    def fetch_sheet_metadata(self, params=None):
        assert params == {"includeGridData": "false"}
        return {
            "sheets": [
                {
                    "properties": {"sheetId": self.sheet_id},
                    "tables": self.tables,
                }
            ]
        }

    def batch_update(self, body):
        self.batch_updates.append(body)


class FakeSheet:
    def __init__(self, rows, tables=None, row_count=10):
        self.id = 123
        self.rows = rows
        self.row_count = row_count
        self.spreadsheet = FakeSpreadsheet(sheet_id=self.id, tables=tables)
        self.added_rows = []
        self.updates = []

    def get(
        self,
        range_name,
        value_render_option=None,
        maintain_size=False,
        pad_values=False,
    ):
        assert maintain_size is True
        assert pad_values is True
        return self.rows

    def col_values(self, column):
        assert column == 1
        return [row[0] for row in self.rows if row and row[0]]

    def add_rows(self, count):
        self.added_rows.append(count)
        self.row_count += count

    def update(self, values, range_name):
        self.updates.append((range_name, values))


def formula_row(location="DEMO"):
    return [
        location,
        "07/10/2026",
        "",
        "Pending",
        "",
        "Footwear",
        "QX1006-500",
        "Aether Shoe",
        "10",
        "",
        180,
        '=IF(K2="","",K2*1.13)',
        "",
        '=IF(M2="","",M2-L2)',
        '=IF(M2="","",N2/M2)',
        "",
    ]


def test_append_row_expands_full_table_by_five_and_copies_format_and_formulas():
    tables = [
        {
            "tableId": "1275550734",
            "range": {
                "startRowIndex": 0,
                "endRowIndex": 2,
                "startColumnIndex": 0,
                "endColumnIndex": 16,
            },
        }
    ]
    sheet = FakeSheet(
        rows=[["Location"] + [""] * 15, formula_row()],
        tables=tables,
        row_count=2,
    )

    append_row("DEMO", "07/10/2026", "Footwear", "QX1006-500", "Aether Shoe", "10", 180, sheet=sheet)

    requests = sheet.spreadsheet.batch_updates[0]["requests"]
    assert requests[0] == {
        "updateTable": {
            "table": {
                "tableId": "1275550734",
                "range": {
                    "sheetId": 123,
                    "startRowIndex": 0,
                    "endRowIndex": 7,
                    "startColumnIndex": 0,
                    "endColumnIndex": 16,
                },
            },
            "fields": "range",
        }
    }
    assert [request["copyPaste"]["pasteType"] for request in requests[1:4]] == [
        "PASTE_FORMAT",
        "PASTE_DATA_VALIDATION",
        "PASTE_FORMULA",
    ]
    assert requests[1]["copyPaste"]["destination"]["startRowIndex"] == 2
    assert requests[1]["copyPaste"]["destination"]["endRowIndex"] == 7
    assert requests[3]["copyPaste"]["source"]["startRowIndex"] == 1
    assert requests[3]["copyPaste"]["destination"]["startRowIndex"] == 2
    assert requests[3]["copyPaste"]["destination"]["endRowIndex"] == 7
    assert [request["repeatCell"]["range"]["startColumnIndex"] for request in requests[4:]] == [
        0,
        12,
        15,
    ]
    assert [request["repeatCell"]["range"]["endColumnIndex"] for request in requests[4:]] == [
        11,
        13,
        16,
    ]
    assert sheet.added_rows == [5]
    assert sheet.updates[0][0] == "A3"
    assert sheet.updates[0][1][0][10] == 180.0


def test_append_row_grows_before_consuming_last_blank_buffer_row():
    tables = [
        {
            "tableId": "1275550734",
            "range": {
                "startRowIndex": 0,
                "endRowIndex": 3,
                "startColumnIndex": 0,
                "endColumnIndex": 16,
            },
        }
    ]
    blank_formula_row = formula_row(location="")
    sheet = FakeSheet(
        rows=[["Location"] + [""] * 15, formula_row(), blank_formula_row],
        tables=tables,
        row_count=3,
    )

    append_row("WH", "07/10/2026", "Footwear", "QX1006-500", "Aether Shoe", "10", 180, sheet=sheet)

    requests = sheet.spreadsheet.batch_updates[0]["requests"]
    assert requests[0]["updateTable"]["table"]["range"]["endRowIndex"] == 8
    assert requests[1]["copyPaste"]["pasteType"] == "PASTE_NORMAL"
    assert requests[1]["copyPaste"]["source"]["startRowIndex"] == 2
    assert requests[1]["copyPaste"]["destination"]["startRowIndex"] == 3
    assert requests[1]["copyPaste"]["destination"]["endRowIndex"] == 8
    assert sheet.updates[0][0] == "A3"


def test_append_row_uses_first_buffer_when_more_than_one_blank_row_remains():
    tables = [
        {
            "tableId": "1275550734",
            "range": {
                "startRowIndex": 0,
                "endRowIndex": 4,
                "startColumnIndex": 0,
                "endColumnIndex": 16,
            },
        }
    ]
    sheet = FakeSheet(
        rows=[
            ["Location"] + [""] * 15,
            formula_row(),
            formula_row(location=""),
            formula_row(location=""),
        ],
        tables=tables,
        row_count=4,
    )

    append_row("WH", "07/10/2026", "Footwear", "QX1006-500", "Aether Shoe", "10", 180, sheet=sheet)

    assert sheet.spreadsheet.batch_updates == []
    assert sheet.updates[0][0] == "A3"


def test_append_row_grows_plain_worksheet_before_writing():
    sheet = FakeSheet(
        rows=[["Location"], ["DEMO"], ["WH"]],
        tables=[],
        row_count=3,
    )

    append_row("ST", "07/10/2026", "Footwear", "QX1006-500", "Aether Shoe", "10", 180, sheet=sheet)

    assert sheet.added_rows == [5]
    assert sheet.updates[0][0] == "A4"
    assert sheet.updates[0][1][0][10] == 180.0
