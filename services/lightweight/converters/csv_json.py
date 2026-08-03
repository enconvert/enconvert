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
        
        if not isinstance(data, list):
            raise ValueError("JSON must be an array of objects for CSV conversion")
        
        if not data:
            raise ValueError("JSON array is empty")
        
        if not isinstance(data[0], dict):
            raise ValueError("JSON array must contain objects (dictionaries)")
        
        headers = list(data[0].keys())
        
        output = StringIO()
        csv_writer = csv.DictWriter(output, fieldnames=headers)
        csv_writer.writeheader()
        csv_writer.writerows(data)
        
        csv_str = output.getvalue()
        
        return csv_str.encode('utf-8')
    
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {str(e)}")
    except Exception as e:
        raise ValueError(f"JSON to CSV conversion failed: {str(e)}")