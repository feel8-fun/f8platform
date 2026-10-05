"""Small management portal bundled with the launcher, requiring no Studio."""
from pathlib import Path
import secrets

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles


def install_portal(app: FastAPI, *, token: str) -> None:
    portal = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @portal.get('/bootstrap/{credential}')
    async def bootstrap(credential: str) -> RedirectResponse:
        if not secrets.compare_digest(credential, token):
            raise HTTPException(status_code=401, detail='Invalid platform credential')
        response = RedirectResponse('/', status_code=303)
        response.set_cookie('f8platform', token, httponly=True, samesite='strict')
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    portal.mount('/', StaticFiles(directory=Path(__file__).parent / 'portal_assets', html=True), name='portal')
    app.mount('/', portal)
