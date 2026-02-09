from __future__ import annotations

from flask import render_template


def page(title: str, template_name: str, **ctx):
    return render_template(template_name, title=title, **ctx)
