
import json
from io import BytesIO, TextIOWrapper

import xmltodict

def json_to_xml(json_bytes: bytes) -> bytes:
    """
    Convert JSON to XML.
    
    Args:
        json_bytes: JSON content as bytes
        
    Returns:
        XML content as bytes
        
    Raises:
        ValueError: If JSON is invalid or conversion fails
    """
    try:
        json_str = json_bytes.decode('utf-8')
        data = json.loads(json_str)
        
        if isinstance(data, list):
            wrapped_data = {'root': {'item': data}}
        elif isinstance(data, dict):
            wrapped_data = {'root': data}
        else:
            wrapped_data = {'root': {'value': data}}
            
        # unparse writes straight into UTF-8 bytes via the wrapper: the str +
        # .encode() path held two full copies of the output at peak. A strict
        # text wrapper (not a raw BytesIO) keeps encode-error behavior and
        # newline handling identical to the old string path.
        buf = BytesIO()
        wrapper = TextIOWrapper(buf, encoding='utf-8', newline='')
        xmltodict.unparse(wrapped_data, output=wrapper, pretty=True, indent=' ')
        wrapper.flush()

        return buf.getvalue()
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {str(e)}")
    except Exception as e:
        raise ValueError(f"JSON to XML conversion failed: {str(e)}")
    
def xml_to_json(xml_bytes: bytes) -> bytes:
    """
    Convert XML to JSON.

    Args:
        xml_bytes: XML content as bytes

    Returns:
        JSON content as bytes

    Raises:
        ValueError: If XML is invalid or conversion fails
    """
    try:
        xml_str = xml_bytes.decode('utf-8')
        data = xmltodict.parse(xml_str)

        # Unwrap root element if it exists
        if isinstance(data, dict) and 'root' in data and len(data) == 1:
            data = data['root']

            # Unwrap item element if it was a list wrapped during json_to_xml
            if isinstance(data, dict) and 'item' in data and len(data) == 1:
                data = data['item']

        # Same single-copy streaming as json_to_xml above.
        buf = BytesIO()
        wrapper = TextIOWrapper(buf, encoding='utf-8', newline='')
        json.dump(data, wrapper, indent=2, ensure_ascii=False)
        wrapper.flush()

        return buf.getvalue()

    except Exception as e:
        raise ValueError(f"XML to JSON conversion failed: {str(e)}")