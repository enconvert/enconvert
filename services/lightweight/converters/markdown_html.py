import markdown

def markdown_to_html(markdown_bytes: bytes) -> bytes:
    """
    Convert Markdown to HTML.
    
    Args:
        markdown_bytes: Markdown content as bytes
        
    Returns:
        HTML content as bytes
        
    Raises:
        ValueError: If Markdown is invalid or conversion fails
    """
    try:
        markdown_str = markdown_bytes.decode('utf-8')
        
        html_str = markdown.markdown(
            markdown_str,
            extensions=['tables', 'fenced_code', 'codehilite', 'toc', 'attr_list']
        )
        
        full_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Converted Document</title>
    <style>
        :root {{
            --bg-color: #ffffff;
            --text-color: #333333;
            --code-bg: #f4f4f4;
            --border-color: #ddd;
            --th-bg: #f4f4f4;
        }}

        [data-theme="dark"] {{
            --bg-color: #1e1e1e;
            --text-color: #e0e0e0;
            --code-bg: #2d2d2d;
            --border-color: #444;
            --th-bg: #2d2d2d;
        }}

        body {{
            max-width: 800px;
            margin: 40px auto;
            padding: 0 20px;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
            line-height: 1.6;
            background-color: var(--bg-color);
            color: var(--text-color);
            transition: background-color 0.3s ease, color 0.3s ease;
        }}

        .theme-toggle {{
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 10px 20px;
            background-color: var(--code-bg);
            border: 1px solid var(--border-color);
            border-radius: 5px;
            cursor: pointer;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
            font-size: 14px;
            color: var(--text-color);
            transition: all 0.3s ease;
            z-index: 1000;
        }}

        .theme-toggle:hover {{
            opacity: 0.8;
        }}

        code {{
            background: var(--code-bg);
            padding: 2px 6px;
            border-radius: 3px;
            font-family: 'Courier New', monospace;
        }}

        pre {{
            background: var(--code-bg);
            padding: 12px;
            border-radius: 5px;
            overflow-x: auto;
        }}

        table {{
            border-collapse: collapse;
            width: 100%;
            margin: 20px 0;
        }}

        th, td {{
            border: 1px solid var(--border-color);
            padding: 8px;
            text-align: left;
        }}

        th {{
            background-color: var(--th-bg);
        }}

        a {{
            color: var(--text-color);
        }}
    </style>
</head>
<body>
<button class="theme-toggle" onclick="toggleTheme()" id="themeToggle">
    <span id="themeIcon">🌙</span> Dark Mode
</button>

{html_str}

<script>
    function toggleTheme() {{
        const html = document.documentElement;
        const currentTheme = html.getAttribute('data-theme');
        const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
        const themeToggle = document.getElementById('themeToggle');
        const themeIcon = document.getElementById('themeIcon');

        html.setAttribute('data-theme', newTheme);
        localStorage.setItem('theme', newTheme);

        if (newTheme === 'dark') {{
            themeIcon.textContent = '☀️';
            themeToggle.childNodes[1].textContent = ' Light Mode';
        }} else {{
            themeIcon.textContent = '🌙';
            themeToggle.childNodes[1].textContent = ' Dark Mode';
        }}
    }}

    // Load saved theme on page load
    (function() {{
        const savedTheme = localStorage.getItem('theme') || 'light';
        const html = document.documentElement;
        const themeToggle = document.getElementById('themeToggle');
        const themeIcon = document.getElementById('themeIcon');

        html.setAttribute('data-theme', savedTheme);

        if (savedTheme === 'dark') {{
            themeIcon.textContent = '☀️';
            themeToggle.childNodes[1].textContent = ' Light Mode';
        }}
    }})();
</script>
</body>
</html>"""

        return full_html.encode('utf-8')
    except UnicodeDecodeError:
        raise ValueError("Invalid Markdown encoding (expected UTF-8)")
    except Exception as e:
        raise ValueError(f"Markdown to HTML conversion failed: {str(e)}")