import csv
import xmltodict
from io import BytesIO, StringIO, TextIOWrapper


def csv_to_xml(csv_bytes: bytes) -> bytes:
    """
    Convert CSV to XML.

    Args:
        csv_bytes: CSV content as bytes

    Returns:
        XML content as bytes

    Raises:
        ValueError: If CSV is invalid or conversion fails
    """
    try:
        csv_str = csv_bytes.decode('utf-8')
        csv_reader = csv.DictReader(StringIO(csv_str))

        data = list(csv_reader)

        if not data:
            raise ValueError("CSV file is empty or has no valid rows")

        wrapped_data = {'root': {'item': data}}
        # unparse writes straight into UTF-8 bytes via the wrapper: the str +
        # .encode() path held two full copies of the output at peak.
        buf = BytesIO()
        wrapper = TextIOWrapper(buf, encoding='utf-8', newline='')
        xmltodict.unparse(wrapped_data, output=wrapper, pretty=True, indent='  ')
        wrapper.flush()

        return buf.getvalue()
    except UnicodeDecodeError:
        raise ValueError("Invalid CSV encoding (expected UTF-8)")
    except Exception as e:
        raise ValueError(f"CSV to XML conversion failed: {str(e)}")


def xml_to_csv(xml_bytes: bytes) -> bytes:
    """
    Convert XML to CSV.

    Args:
        xml_bytes: XML content as bytes

    Returns:
        CSV content as bytes

    Raises:
        ValueError: If XML is invalid or conversion fails
    """
    try:
        xml_str = xml_bytes.decode('utf-8')
        data = xmltodict.parse(xml_str)

        # Unwrap root element if it exists
        if isinstance(data, dict) and len(data) == 1:
            data = list(data.values())[0]

        # Unwrap item element if rows are wrapped
        if isinstance(data, dict) and 'item' in data and len(data) == 1:
            data = data['item']

        # Ensure we have a list of dicts
        if isinstance(data, dict):
            data = [data]

        if not isinstance(data, list):
            raise ValueError("XML structure cannot be converted to CSV")

        if not data or not isinstance(data[0], dict):
            raise ValueError("XML must contain elements with consistent fields for CSV conversion")

        headers = list(data[0].keys())

        output = StringIO()
        csv_writer = csv.DictWriter(output, fieldnames=headers)
        csv_writer.writeheader()
        csv_writer.writerows(data)

        return output.getvalue().encode('utf-8')
    except UnicodeDecodeError:
        raise ValueError("Invalid XML encoding (expected UTF-8)")
    except Exception as e:
        raise ValueError(f"XML to CSV conversion failed: {str(e)}")
