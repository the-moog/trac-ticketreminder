import re

from pkg_resources import resource_filename

from trac.core import *
from trac.attachment import AttachmentModule
from trac.mimeview import Context
from trac.db import DatabaseManager
from trac.env import IEnvironmentSetupParticipant
from trac.web import ITemplateStreamFilter, IRequestHandler, IRequestFilter
from trac.web.chrome import ITemplateProvider, add_stylesheet, add_link, add_ctxtnav, INavigationContributor, add_warning, add_script, Chrome, add_notice
from trac.wiki import format_to_oneliner
from trac.util.datefmt import pretty_timedelta, to_datetime, format_date, get_date_format_hint, format_datetime, parse_date, _time_intervals, to_utimestamp
from trac.util.translation import _
from trac.util import get_reporter_id
from trac.ticket import Ticket
from trac.perm import IPermissionRequestor, PermissionError
from trac.resource import get_resource_url, get_resource_name

from genshi.core import Markup
from genshi.builder import tag
from genshi.filters import Transformer

import db_default

class TicketReminder(Component):
    """
    With this component you can configure reminders for tickets in Trac.".
    """

    implements(IEnvironmentSetupParticipant, ITemplateStreamFilter, ITemplateProvider, IRequestHandler, IRequestFilter, INavigationContributor, IPermissionRequestor)

    # IEnvironmentSetupParticipant methods

    def environment_created(self):
        """Called when a new Trac environment is created."""

        self.found_db_version = 0
        self.upgrade_environment(self.env.get_db_cnx())

    def environment_needs_upgrade(self, db):
        """Called when Trac checks whether the environment needs to be upgraded."""

        cursor = db.cursor()
        cursor.execute("SELECT value FROM system WHERE name=%s", (db_default.name,))
        value = cursor.fetchone()
        if not value:
            self.found_db_version = 0
            return True
        else:
            self.found_db_version = int(value[0])
            if self.found_db_version < db_default.version:
                return True
        
        return False

    def upgrade_environment(self, db):
        """Actually perform an environment upgrade."""

        connector, _ = DatabaseManager(self.env)._get_connector()

        cursor = db.cursor()
        for table in db_default.schema:
            for stmt in connector.to_sql(table):
                cursor.execute(stmt)

        if not self.found_db_version:
            cursor.execute("INSERT INTO system (name, value) VALUES (%s, %s)", (db_default.name, db_default.version))
        else:
            cursor.execute("UPDATE system SET value=%s WHERE name=%s", (db_default.version, db_default.name))

        db.commit()

        self.log.info('Upgraded %s schema version from %d to %d', db_default.name, self.found_db_version, db_default.version)

    # IRequestFilter methods
    def pre_process_request(self, req, handler):
        """Called after initial handler selection, and can be used to change
        the selected handler or redirect request."""

        if self.match_request(req):
            return self
        else:
            return handler

    def post_process_request(req, template, data, content_type):
        """Do any post-processing the request might need; typically adding
        values to the template `data` dictionary, or changing template or
        mime type."""

        return (template, data, content_type)

    # IRequestHandler methods
    def match_request(self, req):
        """Return whether the handler wants to process the given request."""

        match = re.match(r'/ticket/([0-9]+)$', req.path_info)
        if match and req.args.get('action') in ["addreminder", "deletereminder"]:
            req.args['id'] = match.group(1)
            return True

        return False

    def process_request(self, req):
        """Process the request."""

        id = int(req.args.get('id'))

        req.perm('ticket', id).require('TICKET_VIEW')

        if 'TICKET_REMINDER_MODIFY' not in req.perm and 'TICKET_ADMIN' not in req.perm:
            raise PermissionError('TICKET_REMINDER_MODIFY', req.perm._resource, self.env)

        ticket = Ticket(self.env, id)

        if 'cancel' in req.args:
            req.redirect(get_resource_url(self.env, ticket.resource, req.href))

        ticket_name = get_resource_name(self.env, ticket.resource)
        ticket_url = get_resource_url(self.env, ticket.resource, req.href)
        add_link(req, 'up', ticket_url, ticket_name)
        add_ctxtnav(req, _('Back to %(ticket)s', ticket=ticket_name), ticket_url)

        add_stylesheet(req, 'ticketreminder/css/ticketreminder.css')

        if req.args['action'] == "addreminder":
            return self._process_add(req, ticket)
        elif req.args['action'] == "deletereminder":
            return self._process_delete(req, ticket)
        else:
            raise ValueError('Unknown action "%s"' % (req.args['action'],))

    def _process_add(self, req, ticket):
        if req.method == "POST" and self._validate_add(req):
            if req.args.get('type') == 'interval':
                time = clear_time(to_datetime(None))
                delta = _time_intervals[req.args.get('unit')](req.args.get('interval'))
                time += delta
                time = to_utimestamp(time)
            else:
                time = to_utimestamp(parse_date(req.args.get('date')))
            origin = to_utimestamp(to_datetime(None))
            db = self.env.get_db_cnx()
            cursor = db.cursor()
            cursor.execute("INSERT INTO ticketreminder (ticket, time, author, origin, reminded, description) VALUES (%s, %s, %s, %s, 0, %s)", (ticket.id, time, get_reporter_id(req, 'author'), origin, req.args.get('description')))
            db.commit()

            add_notice(req, "Reminder has been added.")
            req.redirect(get_resource_url(self.env, ticket.resource, req.href) + "#reminders")

        add_script(req, 'ticketreminder/js/ticketreminder.js')

        data = {
            'ticket': ticket,
            'date_hint': get_date_format_hint(),
        }

        return ("ticket_reminder_add.html", data, None)
    
    def _validate_add(self, req):
        ty = req.args.get('type')
        if ty == 'interval':
            try:
                req.args['interval'] = int(req.args.get('interval', '').strip())
                if req.args['interval'] <= 0:
                    add_warning(req, "Nonpositive interval value.")
                    return False
            except ValueError:
                add_warning(req, "Invalid or missing interval value.")
                return False

            if req.args.get('unit') not in ['day', 'week', 'month', 'year']:
                add_warning(req, "Please select interval unit.")
                return False

        elif ty == 'date': 
            try:
                time = clear_time(parse_date(req.args.get('date', '').strip()))
                req.args['date'] = format_date(time)
                now = to_datetime(None)
                if time <= now:
                    add_warning(req, "Date value not in the future.")
                    return False
            except TracError:
                add_warning(req, "Invalid or missing date value.")
                return False

        else:
            add_warning(req, "Please select type.")
            return False

        return True

    def _process_delete(self, req, ticket):
        reminder_id = req.args.get('reminder')

        db = self.env.get_db_cnx()
        cursor = db.cursor()

        cursor.execute("SELECT id, time, author, origin, description FROM ticketreminder WHERE id=%s", (reminder_id,))
        reminder = cursor.fetchone()

        if not reminder:
            add_warning(req, "Could not find reminder to delete.")
            req.redirect(get_resource_url(self.env, ticket.resource, req.href))

        if req.method == "POST":
            cursor.execute("DELETE FROM ticketreminder WHERE id=%s", (reminder_id,))
            db.commit()

            add_notice(req, "Reminder has been deleted.")
            req.redirect(get_resource_url(self.env, ticket.resource, req.href) + "#reminders")

        data = {
            'ticket': ticket,
            'formatted_reminder': self._format_reminder(req, ticket, *reminder, delete_button=False),
        }

        return ("ticket_reminder_delete.html", data, None)

    # ITemplateStreamFilter methods

    def filter_stream(self, req, method, filename, stream, data):
        """Return a filtered Genshi event stream, or the original unfiltered stream if no match."""

        if filename == "ticket.html" and ('TICKET_REMINDER_VIEW' in req.perm or 'TICKET_REMINDER_MODIFY' in req.perm or 'TICKET_ADMIN' in req.perm):
            tags = self._reminder_tags(req, data)
            if tags:
                ticket_resource = data['ticket'].resource
                context = Context.from_request(req, ticket_resource)
                attachments_data = AttachmentModule(self.env).attachment_data(context)

                add_stylesheet(req, 'ticketreminder/css/ticketreminder.css')

                # Will attachments section be displayed?
                attachments_or_ticket = Transformer('//div[@id="attachments"]') if attachments_data['can_create'] or attachments_data['attachments'] else Transformer('//div[@id="ticket"]')
                trac_nav = Transformer('//form[@id="propertyform"]/div[@class="trac-nav"]')

                return stream | attachments_or_ticket.after(tags) | trac_nav.append(self._reminder_trac_nav(req, data))

        return stream

    def _get_reminders(self, ticket_id):
        db = self.env.get_db_cnx()
        cursor = db.cursor()

        cursor.execute("SELECT id, time, author, origin, description FROM ticketreminder WHERE ticket=%s AND reminded=0 ORDER BY time", (ticket_id,))
        for row in cursor:
            yield row

    def _format_reminder(self, req, ticket, id, time, author, origin, description, delete_button=True):
        now = to_datetime(None)
        time = to_datetime(time)
        if now >= time:
            when = tag(tag.strong("Right now"), " (pending)")
        else:
            when = tag("In ", tag.strong(pretty_timedelta(time)), " (", format_date(time), ")")

        if description:
            context = Context.from_request(req, ticket.resource)
            desc = tag.div(format_to_oneliner(self.env, context, description), class_="description")
        else:
            desc = tag()

        return tag(self._reminder_delete_form(req, id) if delete_button else None, when, " - added by ", tag.em(Chrome(self.env).authorinfo(req, author)), " ", tag.span(pretty_timedelta(origin), title=format_datetime(origin, req.session.get('datefmt', 'iso8601'), req.tz)), " ago.", desc)

    def _reminder_tags(self, req, data):
        if 'ticket' not in data or not data['ticket'].id:
            return None

        ticket_id = data['ticket'].id
        li_tags = [tag.li(self._format_reminder(req, data['ticket'], *args)) for args in self._get_reminders(ticket_id)]
        if li_tags:
            list_tags = tag.ul(li_tags, class_="reminders")
        else:
            list_tags = []

        if not list_tags and 'TICKET_REMINDER_MODIFY' not in req.perm and 'TICKET_ADMIN' not in req.perm:
            return None

        return \
            tag.div(
                tag.h2("Reminders", class_="foldable"),
                tag.div(
                    list_tags,
                    self._reminder_add_form(req),
                ),
                id="reminders",
            )

    def _reminder_trac_nav(self, req, data):
        return tag(Markup(' <a href="#reminders" title="Go to the list of reminders">Reminders</a> &uarr;'))

    def _reminder_add_form(self, req):
        if 'TICKET_REMINDER_MODIFY' not in req.perm and 'TICKET_ADMIN' not in req.perm:
            return None

        return \
            tag.form(
                tag.div(
                    tag.input(type="hidden", name="action", value="addreminder"),
                    tag.input(type="submit", value="Add reminder"),
                ),
                method="get",
                action="",
                id="addreminder",
            )

    def _reminder_delete_form(self, req, reminder_id):
        if 'TICKET_REMINDER_MODIFY' not in req.perm and 'TICKET_ADMIN' not in req.perm:
            return None

        return \
            tag.form(
                tag.div(
                    tag.input(type="hidden", name="action", value="deletereminder"),
                    tag.input(type="hidden", name="reminder", value=reminder_id),
                    tag.input(type="submit", value="Delete"),
                    class_="inlinebuttons",
                ),
                method="get",
                action="",
            )

    # ITemplateProvider methods

    def get_htdocs_dirs(self):
        """Return a list of directories with static resources (such as style
        sheets, images, etc.)"""

        yield ('ticketreminder', resource_filename(__name__, 'htdocs'))

    def get_templates_dirs(self):
        """Return a list of directories containing the provided template files."""

        yield resource_filename(__name__, 'templates')

    # INavigationContributor methods

    def get_active_navigation_item(self, req):
        """It should return the name of the navigation item that should be highlighted as active/current."""

        return 'tickets'

    def get_navigation_items(self, req):
        """Should return an iterable object over the list of navigation items to
        add, each being a tuple in the form (category, name, text).
        """

        return []

    # IPermissionRequestor methods

    def get_permission_actions(self):
        """Return a list of actions defined by this component."""

        yield 'TICKET_REMINDER_VIEW'
        yield 'TICKET_REMINDER_MODIFY'
    
def clear_time(date):
    return date.replace(hour=0, minute=0, second=0, microsecond=0)
