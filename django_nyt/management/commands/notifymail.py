import logging
import os
import smtplib
import sys
import time
from datetime import timedelta

from django.conf import settings
from django.contrib.sites.models import Site
from django.core import mail
from django.core.management.base import BaseCommand
from django.db.models import F, Q, ExpressionWrapper, DateTimeField, DurationField
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import activate
from django.utils.translation import deactivate

from django_nyt import models
from django_nyt.conf import app_settings

# Daemon / mail loop sleep between each database poll (seconds)
SLEEP_TIME = 120


class Command(BaseCommand):
    can_import_settings = True
    # @ReservedAssignment
    help = (
        "Sends notification emails to subscribed users taking into account "
        "the subscription interval"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--daemon",
            "-d",
            action="store_true",
            dest="daemon",
            help="Go to daemon mode and exit",
        )
        parser.add_argument(
            "--cron",
            "-c",
            action="store_true",
            dest="cron",
            help="Do not loop, just send out emails once and exit",
        )
        parser.add_argument(
            "--pid-file",
            action="store",
            dest="pid",
            help="Where to write PID before exiting",
            default="/tmp/nyt_daemon.pid",
        )
        parser.add_argument(
            "--domain",
            action="store",
            dest="domain",
            help="Base domain to use for URLs (excluding HTTP protocol).",
            default="",
        )
        parser.add_argument(
            "--http-only",
            action="store_true",
            dest="http",
            help="Use http:// in URLs instead of https://.",
            default=False,
        )
        parser.add_argument(
            "--log-file",
            action="store",
            dest="log",
            help="Where daemon should write its log",
            default="/tmp/nyt_daemon.log",
        )
        parser.add_argument(
            "--no-sys-exit",
            action="store_true",
            dest="no_sys_exit",
            help="Skip sys-exit after forking daemon (mainly for testing purposes)",
        )
        parser.add_argument(
            "--daemon-sleep-interval",
            action="store",
            dest="sleep_time",
            help="Minimum sleep between each polling of the database.",
            default=SLEEP_TIME,
        )
        parser.add_argument(
            "--now",
            action="store",
            dest="now",
            help="Simulate when to start sending from (mainly for testing purposes)",
        )

    def _render_and_send(
        self, template_name, template_subject_name, context, connection
    ):
        # This setting overrides everything
        if app_settings.NYT_EMAIL_SUBJECT:
            # Notice that this usually is a lazy translation object
            subject = str(app_settings.NYT_EMAIL_SUBJECT)
        else:
            subject = render_to_string(template_subject_name, context)

        subject = subject.replace("\n", "").strip()

        bodies = {}
        for ext in ['html', 'txt']:
            try:
                # Adjust default naming of templates to drop the default ext
                available_template_name = '%s.%s' % (template_name.rsplit(".", 1)[0], ext)
                bodies[ext] = render_to_string(available_template_name, context).strip()
            except TemplateDoesNotExist:
                # Need at least html or text message body
                if ext == 'txt' and not bodies:
                    raise

        if 'txt' in bodies:
            email = mail.EmailMultiAlternatives(
                subject, bodies['txt'], app_settings.NYT_EMAIL_SENDER,
                [context['user'].email], connection=connection
            )

            if 'html' in bodies:
                email.attach_alternative(bodies['html'], 'text/html')
        else:
            email = mail.EmailMessage(
                subject, bodies['html'], app_settings.NYT_EMAIL_SENDER,
                [context['user'].email], connection=connection
            )
            email.content_subtype = 'html'  # Set main content text/html

        self.logger.info("Sending to: %s" % context['user'].email)
        email.send(fail_silently=False)

    def _daemonize(self):
        self.logger.info("Daemon mode enabled, forking")
        try:
            fpid = os.fork()
            if fpid > 0:
                # Running as daemon now. PID is fpid
                self.logger.info("PID: %s" % str(fpid))
                with open(self.options["pid"], "w") as pid_file:
                    pid_file.write(str(fpid))
                if not self.options["no_sys_exit"]:
                    sys.exit(0)
        except OSError as e:
            sys.stderr.write("fork failed: %d (%s)\n" % (e.errno, e.strerror))
            sys.exit(1)

    def handle(self, *args, **options):  # noqa: max-complexity=12
        # activate the language
        activate(settings.LANGUAGE_CODE)

        options.setdefault("daemon", False)
        options.setdefault("cron", False)
        options.setdefault("no_sys_exit", False)

        self.options = options

        daemon = options["daemon"]
        cron = options["cron"]

        assert not (daemon and cron), "You cannot both choose cron and daemon options"

        self.logger = logging.getLogger("django_nyt")

        if not self.logger.handlers:
            if daemon:
                handler = logging.FileHandler(filename=options["log"])
            else:
                handler = logging.StreamHandler(self.stdout)
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s: %(levelname)s - %(message)s", "%Y-%m-%d %H:%M:%S"
                )
            )
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)

        self.logger.info("Starting django_nyt e-mail dispatcher")

        if not app_settings.NYT_SEND_EMAILS:
            self.logger.info("E-mails disabled - quitting.")
            sys.exit()

        # Run as daemon, ie. fork the process
        if daemon:
            self._daemonize()

        # create a connection to smtp server for reuse
        connection = mail.get_connection()

        if cron:
            if self.options.get("now"):
                now = self.options.get("now")
                self.logger.info(f"using now: {now}")
            else:
                now = timezone.now()

            self.send_mails(connection, now)
            return

        if not daemon:
            print("Entering send-loop, CTRL+C to exit")
        try:
            self.send_loop(connection, int(options["sleep_time"]))
        except KeyboardInterrupt:
            print("\nQuitting...")

        # deactivate the language
        deactivate()

    def send_loop(self, connection, sleep_time):
        # This could be /improved by looking up the last notified person
        last_sent = None

        started_sending_at = datetime.now()
        self.logger.info(
            "Starting send loop at %s" %
            str(started_sending_at))

        while True:
            started_sending_at = timezone.now()
            self.logger.info("Starting send loop at %s" % str(started_sending_at))

            # When we are looping, we don't want to iterate over user_settings that have
            # an interval greater than our last_sent marker.
            if last_sent:
                user_settings = models.Settings.objects.filter(
                    user__is_active=True,
                    interval__lte=((started_sending_at - last_sent).seconds // 60) // 60
                ).order_by("user")
                now = self.options.get("now") or timezone.now()
            else:
                # TOD: This isn't perfect. If we are simulating a "now", we should also make a
                # step-wise approach to incrementing it.
                now = timezone.now()
                user_settings = None

            self.send_mails(
                connection, now, last_sent=last_sent, user_settings=user_settings
            )

            connection.close()
            last_sent = timezone.now()
            time.sleep(sleep_time)

    def _send_with_retry(
        self, template_name, subject_template_name, context, connection, setting
    ):
        """
        Loops through emails in a list of notifications and tries to send
        to each recipient

        """
        # STMP connection send loop
        notifications = context["notifications"]

        if len(context["notifications"]) == 0:
            return

        while True:
            notification_ids = [n.id for n in notifications]
            try:
                self.logger.info(f"Sending to notification ids {notification_ids}")
                # Allow users to disable e-mail notifications from sending (but
                # mark them as sent so they won't be sent in the future)
                if setting.interval > -1:
                    self._render_and_send(
                        template_name, subject_template_name, context, connection
                    )
                for n in notifications:
                    n.is_emailed = True
                    n.save()
                    n.subscription.last_sent = timezone.now()
                    n.subscription.save()
                models.Subscription.objects.filter(
                    notification__id__in=notification_ids
                ).update(last_sent=timezone.now())

                # Track last sent date/time
                setting.last_sent = timezone.now()
                setting.save(update_fields=['last_sent'])

                break
            except smtplib.SMTPSenderRefused:
                self.logger.error(
                    ("E-mail refused by SMTP server ({}), " "skipping!").format(
                        setting.user.email
                    )
                )
                continue
            except smtplib.SMTPException as e:
                self.logger.error(
                    (
                        "You have an error with your SMTP server "
                        "connection, error is: {}"
                    ).format(e)
                )
                self.logger.error("Sleeping for 30s then retrying...")
                time.sleep(30)
            except Exception as e:
                self.logger.error(
                    ("Unhandled exception while sending, giving " "up: {}").format(e)
                )
                raise

    def send_mails(  # noqa: max-complexity=12
        self, connection, now, last_sent=None, user_settings=None
    ):
        """
        Does the lookups and sends out email digests to anyone who has them due.
        Since the system may have different templates depending on which notification is being sent,
        we will generate a call for each template.
        """

        self.logger.debug(f"Entering send_mails(now={now}, last_sent={last_sent}, ...)")

        connection.open()

        if not user_settings:
            # Checks last_sent for each Setting and only send notifications if it's past the interval
            # Builds duration field manually (rather than change Model to make interval a DurationField)
            # Converts interval field to microseconds for DurationField
            user_settings = (
                models.Settings.objects.all()
                .annotate(
                    duration=ExpressionWrapper(F('interval')*60000000, output_field=DurationField())
                )
                .annotate(
                    send_date=ExpressionWrapper(F('last_sent') + F('duration'), output_field=DateTimeField())
                )
                .filter(
                    Q(send_date__lte=timezone.now()) |
                    Q(last_sent__isnull=True)
                )
                .select_related("user")
                .filter(user__is_active=True)
                .prefetch_related(
                    "subscription_set", "subscription_set__notification_type"
                )
            )

        if self.options["domain"]:
            site_object = None
            domain = self.options["domain"]
        else:
            site_object = Site.objects.get_current()
            domain = site_object.domain

        if self.options["http"]:
            http_scheme = "http"
        else:
            http_scheme = "https"

        # We look up what to send for each user's Settings object
        # TODO: Ideally, we should own a lock on each settings object to avoid any double-sending in case
        # this job is running in parallel with another unfinished process. Or a global lock.
        for setting in user_settings:

            threshold = now - timedelta(minutes=setting.interval)

            context = {
                "user": None,
                "username": None,
                "notifications": None,
                "digest": None,
                "site": site_object,
                "domain": domain,
                "http_scheme": http_scheme,
            }

            context["user"] = setting.user
            context["username"] = getattr(setting.user, setting.user.USERNAME_FIELD)
            # get the index of the tuple corresponding to the interval and
            # get the string name
            idx = [y[0] for y in app_settings.NYT_INTERVALS].index(setting.interval)
            context['digest'] = app_settings.NYT_INTERVALS[idx][1]

            emails_per_template = {}

            filter_qs = Q()

            if setting.interval:

                # How much time must have passed since either
                # a) the subscription was created (in case nothing hast been sent)
                # b) the subscription was last active (in case something has been sent)
                filter_qs = Q(Q(created__lte=threshold) & Q(last_sent=None)) | Q(
                    Q(last_sent__lte=threshold) & Q(latest__is_emailed=False)
                )

            # The ordering by notification_type__key is because we want a predictable
            # order currently just for testing purposes.
            for subscription in setting.subscription_set.filter(
                filter_qs, send_emails=True
            ).order_by("notification_type__key"):
                try:
                    template_name = (
                        subscription.notification_type.get_email_template_name()
                    )
                except models.NotificationType.DoesNotExist:
                    self.logger.warning(
                        f"Subscription has non-existent notification type {subscription.notification_type_id}"
                    )
                    continue
                subject_template_name = (
                    subscription.notification_type.get_email_subject_template_name()
                )

                emails_per_template.setdefault(
                    (template_name, subject_template_name), []
                )

                # We assume that if we are sending a digest and we've missed sending it, we can
                # still just summarize ALL notifications that haven't been emailed.
                # Always, no matter what.
                # This also means that if we are sending out emails every 5 minutes, and several
                # notifications have been triggered meanwhile, they'll go into the same email.
                if app_settings.NYT_SEND_ONLY_LATEST:
                    emails_per_template[(template_name, subject_template_name)] += list(
                        subscription.latest,
                    )
                else:
                    emails_per_template[(template_name, subject_template_name)] += list(
                        subscription.notification_set.filter(is_emailed=False),
                    )


            # Send the prepared template names, subjects and context to the user
            for (
                template_name,
                subject_template_name,
            ), notifications in emails_per_template.items():

                if not notifications:
                    continue

                context["notifications"] = notifications

                self._send_with_retry(
                    template_name, subject_template_name, context, connection, setting
                )
