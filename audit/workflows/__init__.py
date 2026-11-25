"""Site-specific workflows. To add one, subclass `audit.workflow.Workflow` and list it here."""

from .coffee import CoffeeWorkflow
from .qantas import QantasWorkflow
from .ticketek import TicketekWorkflow
from .ubereats import UberEatsWorkflow

WORKFLOWS = (CoffeeWorkflow, QantasWorkflow, UberEatsWorkflow, TicketekWorkflow)
