import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from scribe.models.models import ToDo

# initialize the frontend router
router = APIRouter()

# configure the template engine
_templates = Jinja2Templates(directory="templates")
_templates.env.globals["now"] = datetime.datetime.utcnow()


# define the home page route
@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    # render the template file in /templates/home.html
    # we are not currently using this request object
    return _templates.TemplateResponse("pages/home.html.j2", {"request": request})
