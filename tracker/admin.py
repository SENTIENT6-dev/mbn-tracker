"""Admin is only needed for the small lookup lists; everything else has its own screens."""
from django.contrib import admin

from . import models as m

for model in (m.CompanyType, m.StageGate, m.HeritageStatus, m.RoleType):
    admin.site.register(model)
