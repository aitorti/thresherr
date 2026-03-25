from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()

@app.get("/", response_class=HTMLResponse)
def read_root():
    return """
    <html>
        <head><title>Thresherr 🌾</title></head>
        <body>
            <h1>Thresherr is alive! 🌾</h1>
            <p>Ready to start threshing your media library.</p>
        </body>
    </html>
    """
