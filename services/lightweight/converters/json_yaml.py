import json
import yaml

def json_to_yaml(json_bytes: bytes) -> bytes:
    """
    Convert JSON to YAML.
    
    Args:
        json_bytes: JSON content as bytes
        
    Returns:
        YAML content as bytes
        
    Raises:
        ValueError: If JSON is invalid or conversion fails
    """
    try:
        # Raw bytes: json.loads auto-detects UTF-8/16/32 and skips a BOM.
        data = json.loads(json_bytes)
        
        yaml_str = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
        
        return yaml_str.encode('utf-8')
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {str(e)}")
    except Exception as e:
        raise ValueError(f"JSON to YAML conversion failed: {str(e)}")
    
def yaml_to_json(yaml_bytes: bytes ) -> bytes:
    """
    Convert YAML to JSON.
    
    Args:
        yaml_bytes: YAML content as bytes
        
    Returns:
        JSON content as bytes
        
    Raises:
        ValueError: If YAML is invalid or conversion fails
    """
    try:
        # Raw bytes: PyYAML does the spec's own UTF-8/UTF-16 BOM detection.
        data = yaml.safe_load(yaml_bytes)
        
        json_str = json.dumps(data, indent=2, ensure_ascii=False)
        
        return json_str.encode('utf-8')
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML: {str(e)}")
    except Exception as e:
        raise ValueError(f"YAML to JSON conversion failed: {str(e)}")
    