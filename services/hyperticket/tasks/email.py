"""
Notification email tasks — send emails on ticket events.

Uses the platform's mail.py EmailMessage backend.
Triggered by signal handlers when tickets are created, assigned, or updated.
"""

from hyperdjango.logging import logger
from hyperdjango.mail import EmailMessage

from ..models import Agent, Customer, NotificationPreference, Ticket


async def send_notification_email(
    to_email: str,
    subject: str,
    body: str,
) -> bool:
    """Send a notification email. Returns True on success."""
    try:
        msg = EmailMessage(
            subject=subject,
            body=body,
            to=[to_email],
        )
        await msg.send()
        return True
    except Exception as exc:
        logger.error("Email send failed: {err}", err=str(exc))
        return False


async def notify_agent_assigned(ticket: Ticket, agent_id: int) -> None:
    """Send email notification when a ticket is assigned to an agent."""
    agent = await Agent.objects.filter(id=agent_id).first()
    if not agent:
        return

    # Check notification preference
    pref = await NotificationPreference.objects.filter(
        agent_id=agent.id, event_type="ticket_assigned"
    ).first()
    if pref and not pref.channel_email:
        return  # Agent disabled email for this event

    await send_notification_email(
        to_email=agent.email,
        subject=f"Ticket assigned: {ticket.ticket_number} — {ticket.title}",
        body=f"You have been assigned ticket {ticket.ticket_number}.\n\n{ticket.title}\n\n{ticket.description}",
    )


async def notify_customer_status_change(ticket: Ticket) -> None:
    """Send email to customer when their ticket status changes."""
    customer = await Customer.objects.filter(id=ticket.customer_id).first()
    if not customer:
        return

    await send_notification_email(
        to_email=customer.email,
        subject=f"Ticket update: {ticket.ticket_number}",
        body=f"Your ticket {ticket.ticket_number} has been updated.\n\n{ticket.title}",
    )


async def notify_customer_new_ticket(ticket: Ticket) -> None:
    """Send confirmation email when customer submits a ticket."""
    customer = await Customer.objects.filter(id=ticket.customer_id).first()
    if not customer:
        return

    await send_notification_email(
        to_email=customer.email,
        subject=f"Ticket received: {ticket.ticket_number}",
        body=f"We received your request: {ticket.title}\n\nTicket number: {ticket.ticket_number}\n\nWe'll get back to you soon.",
    )
