from __future__ import annotations

import base64
import gzip
import uvicorn

from fastapi import FastAPI, HTTPException, Request
from .linter import (
    LintError,
    LintResponse,
    lint_code,
    convert_lint_error_to_model,
    get_whitelist_patterns,
    settings,
)

app = FastAPI()

@app.post("/lint_base64")
async def lint_base64(request: Request) -> LintResponse:
    """Lint base64-encoded Python code"""
    raw_data = await request.body()

    # Validate request
    if not raw_data:
        raise HTTPException(status_code=400, detail="Empty request body")

    if len(raw_data) > settings.MAX_CODE_SIZE:
        raise HTTPException(status_code=400, detail="Code size too large")

    # Get and validate whitelist patterns
    whitelist_patterns = get_whitelist_patterns(
        request.query_params.get("whitelist_patterns")
    )

    try:
        # Decode base64
        b64_text = raw_data.decode("utf-8", errors="replace")
        code_bytes = base64.b64decode(b64_text)
        code = code_bytes.decode("utf-8", errors="replace")

        if not code.strip():
            raise HTTPException(status_code=400, detail="Empty code")

        # Run linters
        errors = await lint_code(code, whitelist_patterns)

        return LintResponse(
            success=len(errors) == 0,
            errors=[convert_lint_error_to_model(e) for e in errors]
        )
    except Exception as e:
        return LintResponse(
            success=False,
            errors=[convert_lint_error_to_model(
                LintError(message=f"Processing error: {str(e)}")
            )]
        )


@app.post("/lint_gzip")
async def lint_gzip(request: Request) -> LintResponse:
    """Lint gzipped Python code"""
    raw_data = await request.body()

    # Validate request
    if not raw_data:
        raise HTTPException(status_code=400, detail="Empty request body")

    if len(raw_data) > settings.MAX_CODE_SIZE:
        raise HTTPException(status_code=400, detail="Code size too large")

    # Get and validate whitelist patterns
    whitelist_patterns = get_whitelist_patterns(
        request.query_params.get("whitelist_patterns")
    )

    try:
        # Decompress gzip
        code_bytes = gzip.decompress(raw_data)
        code = code_bytes.decode("utf-8", errors="replace")

        if not code.strip():
            raise HTTPException(status_code=400, detail="Empty code")

        # Run linters
        errors = await lint_code(code, whitelist_patterns)

        return LintResponse(
            success=len(errors) == 0,
            errors=[convert_lint_error_to_model(e) for e in errors]
        )
    except Exception as e:
        return LintResponse(
            success=False,
            errors=[convert_lint_error_to_model(
                LintError(message=f"Processing error: {str(e)}")
            )]
        )

def run_server() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8000)
