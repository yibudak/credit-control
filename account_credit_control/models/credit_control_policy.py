# Copyright 2012-2017 Camptocamp SA
# Copyright 2017 Okia SPRL (https://okia.be)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from math import copysign
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from datetime import timedelta

CHANNEL_LIST = [
    ("letter", "Letter"),
    ("email", "Email"),
    ("phone", "Phone"),
]


class CreditControlPolicy(models.Model):
    """Define a policy of reminder"""

    _name = "credit.control.policy"
    _description = """Define a reminder policy"""

    name = fields.Char(
        required=True,
    )
    level_ids = fields.One2many(
        comodel_name="credit.control.policy.level",
        inverse_name="policy_id",
        string="Policy Levels",
    )
    do_nothing = fields.Boolean(
        help="For policies which should not " "generate lines or are obsolete",
    )
    company_id = fields.Many2one(
        comodel_name="res.company",
        string="Company",
    )
    account_ids = fields.Many2many(
        comodel_name="account.account",
        string="Accounts",
        required=True,
        domain="[('internal_type', '=', 'receivable')]",
        help="This policy will be active only" " for the selected accounts",
    )
    active = fields.Boolean(
        default=True,
    )
    policy_sign = fields.Selection(
        selection=[
            ("positive", "Positive"),
            ("negative", "Negative"),
        ],
        compute="_compute_policy_sign",
    )

    @api.depends("level_ids")
    def _compute_policy_sign(self):
        mapping = {-1: "negative", 0: False, 1: "positive"}
        for record in self:
            policy_sign = 0
            for level in record.level_ids:
                if not policy_sign:
                    policy_sign = copysign(1, level.delay_days)
                elif copysign(1, level.delay_days) != policy_sign:
                    policy_sign = 0
                    break
            record.policy_sign = mapping[policy_sign]

    @api.multi
    def _move_lines_domain(self, credit_control_run):
        """Build the default domain for searching move lines"""
        self.ensure_one()
        domain = [
            ("account_id", "in", self.account_ids.ids),
            ("reconciled", "=", False),
            ("partner_id", "!=", False),
            ("company_id", "=", credit_control_run.company_id.id),
        ]
        # We need to set the company in order to work properly with multi-companies.
        # If we have Company A and Company B (child of A), we might be able to run this
        # for company B from Company A view.
        if self.policy_sign == "negative":
            domain.append(("date_maturity", ">=", credit_control_run.date))
        elif self.policy_sign == "positive":
            domain.append(("date_maturity", "<=", credit_control_run.date))
        return domain

    @api.multi
    @api.returns("account.move.line")
    def _due_move_lines(self, credit_control_run):
        """Get the due move lines for the policy of the company.

        The set of ids will be reduced and extended according
        to the specific policies defined on partners and invoices.

        Do not use direct SQL in order to respect security rules.

        Assume that only the receivable lines have a maturity date and that
        accounts used in the policy are reconcilable.
        """
        self.ensure_one()
        move_l_obj = self.env["account.move.line"]
        if credit_control_run.company_id.credit_policy_id.id != self.id:
            return move_l_obj
        domain_line = self._move_lines_domain(credit_control_run)
        return move_l_obj.search(domain_line)

    @api.multi
    @api.returns("account.move.line")
    def _move_lines_subset(self, credit_control_run, model, move_relation_field):
        """Get the move lines related to one model for a policy.

        Do not use direct SQL in order to respect security rules.

        Assume that only the receivable lines have a maturity date and that
        accounts used in the policy are reconcilable.

        The policy relation field must be named credit_policy_id.

        :param str controlling_date: date of credit control
        :param str model: name of the model where is defined a credit_policy_id
        :param str move_relation_field: name of the field in account.move.line
            which is a many2one to `model`
        :return: recordset to add in the process, recordset to remove from
            the process
        """
        self.ensure_one()
        # MARK possible place for a good optimisation
        my_obj = self.env[model].with_context(active_test=False)
        default_domain = self._move_lines_domain(credit_control_run)

        to_add = self.env["account.move.line"]
        to_remove = self.env["account.move.line"]

        # The lines which are linked to this policy have to be included in the
        # run for this policy.
        # If another object override the credit_policy_id (ie. invoice after
        add_objs = my_obj.search([("credit_policy_id", "=", self.id)])
        if add_objs:
            domain = list(default_domain)
            domain.append((move_relation_field, "in", add_objs.ids))
            to_add = to_add.search(domain)

        # The lines which are linked to another policy do not have to be
        # included in the run for this policy.
        neg_objs = my_obj.search(
            [("credit_policy_id", "!=", self.id), ("credit_policy_id", "!=", False)]
        )
        if neg_objs:
            domain = list(default_domain)
            domain.append((move_relation_field, "in", neg_objs.ids))
            to_remove = to_remove.search(domain)
        return to_add, to_remove

    @api.multi
    @api.returns("account.move.line")
    def _get_partner_related_lines(self, credit_control_run):
        """Get the move lines for a policy related to a partner.

        :param str controlling_date: date of credit control
        :return: recordset to add in the process, recordset to remove from
            the process
        """
        return self._move_lines_subset(
            credit_control_run,
            "res.partner",
            "partner_id",
        )

    @api.multi
    @api.returns("account.move.line")
    def _get_invoice_related_lines(self, credit_control_run):
        """Get the move lines for a policy related to an invoice.

        :param str controlling_date: date of credit control
        :return: recordset to add in the process, recordset to remove from
            the process
        """
        return self._move_lines_subset(
            credit_control_run,
            "account.invoice",
            "invoice_id",
        )

    @api.multi
    @api.returns("account.move.line")
    def _get_move_lines_to_process(self, credit_control_run):
        """Build a list of move lines ids to include in a run
        for a policy at a given date.

        :param str controlling_date: date of credit control
        :return: recordset to include in the run
        """
        self.ensure_one()
        # there is a priority between the lines, depicted by the calls below
        lines = self._due_move_lines(credit_control_run)
        to_add, to_remove = self._get_partner_related_lines(credit_control_run)
        lines = (lines | to_add) - to_remove
        to_add, to_remove = self._get_invoice_related_lines(credit_control_run)
        lines = (lines | to_add) - to_remove
        return lines

    @api.multi
    @api.returns("account.move.line")
    def _lines_different_policy(self, lines):
        """Return a set of move lines ids for which there is an
        existing credit line but with a different policy.
        """
        self.ensure_one()
        different_lines = self.env["account.move.line"]
        if not lines:
            return different_lines
        cr = self.env.cr
        cr.execute(
            "SELECT move_line_id FROM credit_control_line"
            "    WHERE policy_id != %s and move_line_id in %s"
            "    AND manually_overridden IS false",
            (self.id, tuple(lines.ids)),
        )
        res = cr.fetchall()
        if res:
            return different_lines.browse([row[0] for row in res])
        return different_lines

    @api.multi
    def check_policy_against_account(self, account):
        """Ensure that the policy corresponds to account relation"""
        allowed = self.search(
            ["|", ("account_ids", "in", account.ids), ("do_nothing", "=", True)]
        )
        if self not in allowed:
            allowed_names = "\n".join(x.name for x in allowed)
            raise UserError(
                _(
                    "You can only use a policy set on "
                    "account %s.\n"
                    "Please choose one of the following "
                    "policies:\n %s"
                )
                % (account.name, allowed_names)
            )
        return True

    @api.multi
    def _generate_credit_lines(self, credit_control_run, default_lines_vals=None):
        self.ensure_one()
        controlling_date = credit_control_run.date
        credit_line_model = self.env["credit.control.line"]
        lines = self._get_move_lines_to_process(credit_control_run)
        manual_lines = self._lines_different_policy(lines)
        lines -= manual_lines
        policy_lines_generated = credit_line_model
        if lines:
            # policy levels are sorted by level
            # so iteration is in the correct order
            create = policy_lines_generated.create_or_update_from_mv_lines
            for level in reversed(self.level_ids):
                level_lines = level.get_level_lines(controlling_date, lines)
                policy_lines_generated += create(
                    level_lines,
                    level,
                    controlling_date,
                    credit_control_run.company_id,
                    default_lines_vals=default_lines_vals,
                )
        if policy_lines_generated:
            report = _(
                'Policy "<b>%s</b>" has generated <b>%d Credit '
                "Control Lines.</b><br/>"
            ) % (self.name, len(policy_lines_generated))
        else:
            report = (
                _(
                    'Policy "<b>%s</b>" has not generated any '
                    "Credit Control Lines.<br/>"
                )
                % self.name
            )
        return (manual_lines, policy_lines_generated, report)


class CreditControlPolicyLevel(models.Model):
    """Define a policy level. A level allows to determine if
    a move line is due and the level of overdue of the line"""

    _name = "credit.control.policy.level"
    _order = "level"
    _description = """A credit control policy level"""

    name = fields.Char(
        required=True,
        translate=True,
    )
    policy_id = fields.Many2one(
        comodel_name="credit.control.policy",
        string="Related Policy",
        required=True,
        ondelete="cascade",
    )
    level = fields.Integer(
        required=True,
    )
    computation_mode = fields.Selection(
        selection=[
            ("net_days", "Due Date"),
            ("end_of_month", "Due Date, End Of Month"),
            ("previous_date", "Previous Reminder"),
        ],
        string="Compute Mode",
        required=True,
    )
    delay_days = fields.Integer(
        string="Delay (in days)",
        required=True,
    )
    email_template_id = fields.Many2one(
        comodel_name="mail.template",
        string="Email Template",
        required=True,
    )
    channel = fields.Selection(
        selection=CHANNEL_LIST,
        required=True,
    )
    custom_text = fields.Html(
        string="Custom Message",
        required=True,
        translate=True,
    )
    custom_mail_text = fields.Html(
        string="Custom Mail Message",
        required=True,
        translate=True,
    )
    custom_text_after_details = fields.Html(
        string="Custom Message after details",
        translate=True,
    )

    _sql_constraint = [
        ("unique level", "UNIQUE (policy_id, level)", "Level must be unique per policy")
    ]

    @api.multi
    @api.constrains("level", "computation_mode")
    def _check_level_mode(self):
        """The smallest level of a policy cannot be computed on the
        "previous_date".
        """
        for policy_level in self:
            smallest_level = self.search(
                [("policy_id", "=", policy_level.policy_id.id)],
                order="level asc",
                limit=1,
            )
            if smallest_level.computation_mode == "previous_date":
                raise ValidationError(
                    _("The smallest level can not be " "of type Previous Reminder")
                )

    @api.multi
    def _previous_level(self):
        """For one policy level, returns the id of the previous level

        If there is no previous level, it returns None, it means that's the
        first policy level

        :return: previous level or None if there is no previous level
        """
        self.ensure_one()
        previous_levels = self.search(
            [
                ("policy_id", "=", self.policy_id.id),
                ("level", "<", self.level),
            ],
            order="level desc",
            limit=1,
        )
        if not previous_levels:
            return None
        return previous_levels

    # ----- sql time related methods ---------

    @staticmethod
    def _net_days_get_boundary(controlling_date, policy_id):
        return [
            (
                "date_maturity",
                "<=",
                controlling_date - timedelta(days=policy_id.delay_days),
            ),
        ]

    @staticmethod
    def _end_of_month_get_boundary(controlling_date, policy_id):
        raise NotImplementedError
        # return (
        #     "(date_trunc('MONTH', (mv_line.date_maturity + %(delay)s))+"
        #     "INTERVAL '1 MONTH - 1 day')::date"
        #     "<= date(%(controlling_date)s)"
        # )

    @staticmethod
    def _previous_date_get_boundary(controlling_date, policy_id):
        raise NotImplementedError
        # return "(cr_line.date + %(delay)s)::date <= date(%(controlling_date)s)"

    @api.multi
    def _get_sql_date_boundary_for_computation_mode(self, controlling_date):
        """Return a where clauses statement for the given controlling
        date and computation mode of the level
        """
        self.ensure_one()
        fname = "_%s_get_boundary" % (self.computation_mode,)
        if hasattr(self, fname):
            fnc = getattr(self, fname)
            return fnc(controlling_date, self)
        else:
            raise NotImplementedError(
                _("Can not get function for computation mode: " "%s is not implemented")
                % (fname,)
            )

    @api.multi
    def _get_sql_level_part(self):
        """Return a where clauses statement for the previous line level"""
        self.ensure_one()
        previous_level = self._previous_level()
        if previous_level:
            return "cr_line.level = %s" % (previous_level.level,)
        else:
            return "cr_line.id IS NULL"

    # -----------------------------------------

    @api.multi
    @api.returns("account.move.line")
    def _get_level_move_lines(self, controlling_date, lines):
        """Retrieve the move lines for all levels."""
        self.ensure_one()
        move_line_obj = self.env["account.move.line"]
        if not lines:
            return move_line_obj

        domain = [
            ("id", "in", lines.ids),
            ("debit", "!=", 0.0),
        ]
        # Add date boundary
        domain += self._get_sql_date_boundary_for_computation_mode(controlling_date)
        suitable_lines = move_line_obj.search(domain)
        return suitable_lines

    @api.multi
    @api.returns("account.move.line")
    def get_level_lines(self, controlling_date, lines):
        """get all move lines in entry lines that match the current level"""
        self.ensure_one()
        matching_lines = self.env["account.move.line"]
        matching_lines |= self._get_level_move_lines(controlling_date, lines)
        return matching_lines
