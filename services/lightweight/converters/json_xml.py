
import json
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
            
        xml_str = xmltodict.unparse(wrapped_data, pretty=True, indent=' ')
        
        return xml_str.encode('utf-8')
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

        json_str = json.dumps(data, indent=2, ensure_ascii=False)

        return json_str.encode('utf-8')

    except Exception as e:
        raise ValueError(f"XML to JSON conversion failed: {str(e)}")