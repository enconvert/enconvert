import json
import csv
from io import BytesIO, StringIO, TextIOWrapper

def csv_to_json(csv_bytes: bytes) -> bytes:
    """
    Convert CSV to JSON.
    
    Args:
        csv_bytes: CSV content as bytes
        
    Returns:
        JSON array content as bytes
        
    Raises:
        ValueError: If CSV is invalid or conversion fails
    """
    try:
        csv_str = csv_bytes.decode('utf-8')
        csv_reader = csv.DictReader(StringIO(csv_str))
        
        data = list(csv_reader)
        
        if not data:
            raise ValueError("CSV file is empty or has no valid rows")
        
        # Stream JSON straight into UTF-8 bytes: dumps() + .encode() held two
        # full copies of the output at peak. newline='' keeps bytes identical.
        buf = BytesIO()
        wrapper = TextIOWrapper(buf, encoding='utf-8', newline='')
        json.dump(data, wrapper, indent=2, ensure_ascii=False)
        wrapper.flush()

        return buf.getvalue()
    except UnicodeDecodeError:
        raise ValueError("Invalid CSV encoding (expected UTF-8)")
    except ValueError:
        # Same doubling as json_to_csv: "CSV to JSON conversion failed:
        # CSV file is empty or has no valid rows".
        raise
    except Exception as e:
        raise ValueError(f"CSV to JSON conversion failed: {str(e)}")
    
def json_to_csv(json_bytes: bytes) -> bytes:
    """
    Convert JSON to CSV.
    
    Args:
        json_bytes: JSON array content as bytes
        
    Returns:
        CSV content as bytes
        
    Raises:
        ValueError: If JSON is invalid or conversion fails
    """
    try:
        json_str = json_bytes.decode('utf-8')
        data = json.loads(json_str)

        # A single object is one row. Rejecting it forced callers to wrap
        # their own payload in brackets for no reason.
        if isinstance(data, dict):
            data = [data]

        if not isinstance(data, list):
            raise ValueError(
                "JSON must be an array of objects (or a single object) for "
                f"CSV conversion; got {type(data).__name__}"
            )

        if not data:
            raise ValueError("JSON array is empty")

        non_objects = [
            i for i, row in enumerate(data) if not isinstance(row, dict)
        ]
        if non_objects:
            raise ValueError(
                "JSON array must contain objects (dictionaries); "
                f"item {non_objects[0]} is "
                f"{type(data[non_objects[0]]).__name__}"
            )

        # Union of every row's keys, first-seen order. Reading headers off
        # row 0 alone made any later row with an extra key raise
        # "dict contains fields not in fieldnames" from DictWriter — a
        # ragged but perfectly valid JSON array could not be converted.
        headers: list[str] = []
        seen: set[str] = set()
        for row in data:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    headers.append(str(key))

        output = StringIO()
        # restval fills the gaps a ragged array leaves.
        csv_writer = csv.DictWriter(output, fieldnames=headers, restval='')
        csv_writer.writeheader()
        csv_writer.writerows(data)

        csv_str = output.getvalue()

        return csv_str.encode('utf-8')

    except UnicodeDecodeError:
        raise ValueError("Invalid JSON encoding (expected UTF-8)")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {str(e)}")
    except ValueError:
        # Our own diagnostics above are already the final message. Letting
        # them fall through to the generic handler produced doubled text:
        # "JSON to CSV conversion failed: JSON must be an array of objects
        # for CSV conversion".
        raise
    except Exception as e:
        raise ValueError(f"JSON to CSV conversion failed: {str(e)}")