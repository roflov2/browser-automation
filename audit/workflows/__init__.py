"""Site-specific workflows. To add one, subclass `audit.workflow.Workflow` and list it here."""

from .coffee import CoffeeWorkflow
from .ticketek import TicketekWorkflow
from .ubereats import UberEatsWorkflow
from .virgin import VirginWorkflow

WORKFLOWS = (CoffeeWorkflow, VirginWorkflow, UberEatsWorkflow, TicketekWorkflow)
