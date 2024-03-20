# Copyright 2012-2022 Akretion France (http://www.akretion.com)
# @author Alexis de Lattre <alexis.delattre@akretion.com>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools.mimetypes import guess_mimetype
from datetime import datetime, date as datelib
import csv
from tempfile import NamedTemporaryFile
from collections import OrderedDict
import base64
import logging

logger = logging.getLogger(__name__)
try:
    import openpyxl  # for XLSX
except ImportError:
    logger.debug('Cannot import openpyxl')
try:
    import xlrd  # for XLS
except ImportError:
    logger.debug('Cannot import xlrd')
try:
    import rows  # for ODS... but could be later used for XLS, XLSX, CSV
    # But they must make a new release first https://github.com/turicas/rows/issues/368
except ImportError:
    rows = None
    logger.debug('Cannot import rows')


GENERIC_CSV_DEFAULT_DATE = '%d/%m/%Y'
DELIMITER = {
    'coma': ',',
    'semicolon': ';',
    'tab': '\t',
    }


class AccountMoveImport(models.TransientModel):
    _name = "account.move.import"
    _description = "Import account move from file"
    _check_company_auto = True

    company_id = fields.Many2one(
        'res.company', string='Company',
        required=True, default=lambda self: self.env.company)
    file_to_import = fields.Binary(string='File to Import')
    filename = fields.Char()
    file_format = fields.Selection([
        ('genericxlsx', 'Generic XLSX/XLS/ODS'),
        ('genericcsv', 'Generic CSV'),
        ('fec_txt', 'FEC (text)'),
        ('nibelis', 'Nibelis (Prisme)'),
        ('quadra', 'Quadra (without analytic)'),
        ('extenso', 'In Extenso'),
        ('cielpaye', 'Ciel Paye'),
        ('payfit', 'Payfit'),
        ], string='File Format', required=True, default='genericxlsx')
    post_move = fields.Boolean(
        string='Post and Reconcile',
        help="If enabled, the journal entries will be posted and, if the Reconcile Ref "
        "is available in the import file, Odoo will reconcile the journal items.")
    force_journal_id = fields.Many2one(
        'account.journal', string="Force Journal",
        domain="[('company_id', '=', company_id)]", check_company=True,
        help="Journal in which the journal entry will be created, "
        "even if the file indicate another journal.")
    force_move_ref = fields.Char('Force Reference')
    force_move_line_name = fields.Char('Force Label')
    force_move_date = fields.Date('Force Date')
    file_encoding = fields.Selection([
        ('ascii', 'ASCII'),
        ('latin1', 'ISO 8859-15 (alias Latin1)'),
        ('utf-8', 'UTF-8'),
        ], string='File Encoding', default='utf-8')
    delimiter = fields.Selection([
        ('coma', 'Coma'),
        ('semicolon', 'Semicolon'),
        ('tab', 'Tab'),
        ], default='coma', string="Field Delimiter")
    # technical fields
    force_move_date_required = fields.Boolean(compute='_compute_force_required')
    force_journal_required = fields.Boolean(compute='_compute_force_required')
    advanced_options = fields.Boolean()
    # START GENERIC advanced options
    date_by_move_line = fields.Boolean(
        string='Is date by move line ?',
        help="If enabled, we don't use date to detect the split "
        "of journal entries.")
    skip_null_lines = fields.Boolean(
        string="Skip lines with debit = credit = 0")
    keep_odoo_move_name = fields.Boolean(
        string="Don't Force Journal Entry Name",
        help="If 'move_name' is present in the pivot format and "
        "this option is enabled, it will ignore the value of 'move_name' "
        "and use the sequence generated by Odoo when posting the journal entry.")
    split_move_method = fields.Selection([
        ('balanced', 'Balanced'),
        ('move_name', 'Journal Entry Number'),
        ], default='balanced', required=True,
        help="If you select the method 'Balanced', Odoo will cut the move when a group of lines is balanced with the same journal and date. If you select the method 'Journal Entry Number', Odoo will cut the move using the field 'move_name' of the pivot format (this field is optional, but it will have to be present if you select this method).")
    # START advanced options used in 'genericcsv' import
    # (but could be used by other imports if needed)
    date_format = fields.Char(
        default=GENERIC_CSV_DEFAULT_DATE,
        required=True)
    file_with_header = fields.Boolean(
        string='Has Header Line',
        help="Indicate if the first line is a header line and should be ignored.")

    @api.depends('file_format')
    def _compute_force_required(self):
        for wiz in self:
            force_move_date_required = False
            force_journal_required = False
            if wiz.file_format == 'payfit':
                force_move_date_required = True
                force_journal_required = True
            wiz.force_move_date_required = force_move_date_required
            wiz.force_journal_required = force_journal_required

    def button_show_advanced_options(self):
        return self._set_advanced_options(True)

    def button_hide_advanced_options(self):
        return self._set_advanced_options(False)

    def _set_advanced_options(self, advanced_options):
        self.ensure_one()
        self.write({'advanced_options': advanced_options})
        action = self.env["ir.actions.actions"]._for_xml_id(
            "account_move_csv_import.account_move_import_action")
        action['res_id'] = self.id
        return action

    # PIVOT FORMAT
    # [{
    #    'account': '411000',
    #    'analytic': 'ADM',  # analytic account code (100% distribution)
    # OR 'analytic': 'ADM:39.4,SUPP:60.6',  # analytic distribution
    #    'partner': 'R1242',
    #    'name': 'label',  # optional, for account.move.line
    #    'credit': 12.42,
    #    'debit': 0,
    #    'ref': '9804',  # optional
    #    'journal': 'VT',  # journal code
    #    'date': '2017-02-15',  # also accepted in datetime format
    #    'ref: 'X12',
    #    'move_name': 'OD/2022/1242',  # optional, for 'name' of account.move
    #                                  # only used when keep_odoo_move_name = False
    #    'reconcile_ref': 'A1242',  # will be written in import_reconcile
    #                               # and be processed after move line creation
    #    'line': 2,  # Line number for error messages.
    #                # Must be the line number including headers
    # },
    #  2nd line...
    #  3rd line...
    # ]

    def file2pivot(self, fileobj, file_bytes):
        file_format = self.file_format
        if file_format == 'nibelis':
            return self.nibelis2pivot(fileobj)
        elif file_format == 'genericcsv':
            return self.genericcsv2pivot(fileobj)
        elif file_format == 'genericxlsx':
            return self.genericxlsx_autodetect(fileobj, file_bytes)
        elif file_format == 'quadra':
            return self.quadra2pivot(file_bytes)
        elif file_format == 'extenso':
            return self.extenso2pivot(fileobj)
        elif file_format == 'payfit':
            return self.payfit2pivot(fileobj)
        elif file_format == 'cielpaye':
            return self.cielpaye2pivot(fileobj)
        elif file_format == 'fec_txt':
            return self.fectxt2pivot(fileobj)
        else:
            raise UserError(_("You must select a file format."))

    def run_import(self):
        self.ensure_one()
        if not self.file_to_import:
            raise UserError(_("You must upload a file to import."))
        fileobj = NamedTemporaryFile('wb+', prefix='odoo-move_import-', suffix='.xlsx')
        file_bytes = base64.b64decode(self.file_to_import)
        fileobj.write(file_bytes)
        fileobj.seek(0)  # We must start reading from the beginning !
        pivot = self.file2pivot(fileobj, file_bytes)
        fileobj.close()
        logger.debug('pivot before update: %s', pivot)
        self.clean_strip_pivot(pivot)
        self.update_pivot(pivot)
        moves = self.create_moves_from_pivot(pivot, post=self.post_move)
        if self.post_move:
            self.reconcile_move_lines(moves)
        action = self.env["ir.actions.actions"]._for_xml_id(
            "account.action_move_journal_line")
        # We need to remove from context 'search_default_posted': 1
        action['context'] = {'default_move_type': 'entry', 'view_no_maturity': True}
        if len(moves) == 1:
            action.update({
                'view_mode': 'form,tree',
                'res_id': moves[0].id,
                'view_id': False,
                'views': False,
                })
        else:
            action.update({
                'view_mode': 'tree,form',
                'domain': [('id', 'in', moves.ids)],
                })
        return action

    def clean_strip_pivot(self, pivot):
        for l in pivot:
            for key, value in l.items():
                if value:
                    if isinstance(value, str):
                        l[key] = value.strip() or False
                else:
                    l[key] = False

    def update_pivot(self, pivot):
        force_move_date = self.force_move_date
        force_move_ref = self.force_move_ref
        force_move_line_name = self.force_move_line_name
        force_journal_code =\
            self.force_journal_id and self.force_journal_id.code or False
        for l in pivot:
            if force_move_date:
                l['date'] = force_move_date
            if force_move_line_name:
                l['name'] = force_move_line_name
            if force_move_ref:
                l['ref'] = force_move_ref
            if force_journal_code:
                l['journal'] = force_journal_code
            if not l['credit']:
                l['credit'] = 0.0
            if not l['debit']:
                l['debit'] = 0.0

    def extenso2pivot(self, fileobj):
        fieldnames = [
            'journal', 'date', False, 'account', False, False, False, False,
            'debit', 'credit']
        res = []
        with open(fileobj.name, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(
                f,
                fieldnames=fieldnames,
                delimiter='\t',
                quoting=csv.QUOTE_MINIMAL)
            i = 0
            for l in reader:
                i += 1
                l['credit'] = l['credit'] or '0'
                l['debit'] = l['debit'] or '0'
                vals = {
                    'journal': l['journal'],
                    'account': l['account'],
                    'credit': float(l['credit'].replace(',', '.')),
                    'debit': float(l['debit'].replace(',', '.')),
                    'date': datetime.strptime(l['date'], '%d%m%Y'),
                    'line': i,
                }
                res.append(vals)
        return res

    def cielpaye2pivot(self, fileobj):
        fieldnames = [
            False, 'journal', 'date', 'account', False, 'amount', 'sign',
            False, 'name', False]
        res = []
        with open(fileobj.name, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(
                f,
                fieldnames=fieldnames,
                delimiter='\t',
                quoting=csv.QUOTE_MINIMAL)
            i = 0
            for l in reader:
                i += 1
                # skip non-move lines
                if l.get('date') and l.get('name') and l.get('amount'):
                    amount = float(l['amount'].replace(',', '.'))
                    vals = {
                        'journal': l['journal'],
                        'account': l['account'],
                        'credit': l['sign'] == 'C' and amount or 0,
                        'debit': l['sign'] == 'D' and amount or 0,
                        'date': datetime.strptime(l['date'], '%d/%m/%Y'),
                        'name': l['name'],
                        'line': i,
                    }
                    res.append(vals)
        return res

    def fectxt2pivot(self, fileobj):
        fieldnames = [
            'journal',        # JournalCode
            False,            # JournalLib
            'move_name',      # EcritureNum
            'date',           # EcritureDate
            'account',        # CompteNum
            False,            # CompteLib
            'partner_ref',    # CompAuxNum
            False,            # CompAuxLib
            'ref',            # PieceRef
            False,            # PieceDate
            'name',           # EcritureLib
            'debit',          # Debit
            'credit',         # Credit
            'reconcile_ref',  # EcritureLet
            False,            # DateLet
            False,            # ValidDate
            False,            # Montantdevise
            False,            # Idevise
            ]
        res = []
        first_line = fileobj.readline().decode()
        dialect = csv.Sniffer().sniff(first_line, delimiters="|\t")
        fileobj.seek(0)
        with open(fileobj.name, newline='', encoding=self.file_encoding) as f:
            reader = csv.DictReader(
                f,
                fieldnames=fieldnames,
                delimiter=dialect.delimiter)
            i = 0
            for l in reader:
                i += 1
                # Skip header line
                if i == 1:
                    continue
                l['credit'] = l['credit'] or '0'
                l['debit'] = l['debit'] or '0'
                vals = {
                    'journal': l['journal'],
                    'move_name': l['move_name'],
                    'account': l['account'],
                    'partner': l['partner_ref'],
                    'credit': float(l['credit'].replace(',', '.')),
                    'debit': float(l['debit'].replace(',', '.')),
                    'date': datetime.strptime(l['date'], '%Y%m%d'),
                    'name': l['name'],
                    'ref': l['ref'],
                    'reconcile_ref': l['reconcile_ref'],
                    'line': i,
                }
                res.append(vals)
        return res

    def genericcsv2pivot(self, fileobj):
        # Prisme
        fieldnames = [
            'date', 'journal', 'account', 'partner',
            'analytic', 'name', 'debit', 'credit',
            'ref', 'reconcile_ref'
            ]
        # I use utf-8-sig instead of utf-8 to transparently handle BOM
        # https://en.wikipedia.org/wiki/Byte_order_mark
        encoding = self.file_encoding == 'utf-8' and 'utf-8-sig' or self.file_encoding
        res = []
        with open(fileobj.name, newline='', encoding=encoding) as f:
            reader = csv.DictReader(
                f,
                fieldnames=fieldnames,
                delimiter=DELIMITER[self.delimiter],
                quotechar='"',
                quoting=csv.QUOTE_MINIMAL)
            i = 0
            for l in reader:
                i += 1
                if i == 1 and self.file_with_header:
                    continue
                date_str = l['date']
                try:
                    date = datetime.strptime(date_str, self.date_format)
                except Exception:
                    raise UserError(_(
                        "Date parsing error: '%s' in line %s does not match "
                        "date format '%s'.") % (date_str, i, self.date_format))

                vals = {
                    'journal': l['journal'],
                    'account': l['account'],
                    'credit': float(l['credit'].replace(',', '.') or 0),
                    'debit': float(l['debit'].replace(',', '.') or 0),
                    'date': date,
                    'name': l['name'],
                    'ref': l.get('ref', ''),
                    'reconcile_ref': l.get('reconcile_ref', ''),
                    'line': i,
                    }
                if l['analytic']:
                    vals['analytic'] = l['analytic']
                if l['partner']:
                    vals['partner'] = l['partner']
                res.append(vals)
        return res

    def genericxlsx_autodetect(self, fileobj, file_bytes):
        mime_res = guess_mimetype(file_bytes)
        if mime_res == 'application/vnd.oasis.opendocument.spreadsheet':  # ODS
            return self.genericods2pivot(fileobj)
        elif mime_res == 'application/vnd.ms-excel':  # XLS
            return self.genericxls2pivot(fileobj)
        elif mime_res == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':  # XLSX
            return self.genericxlsx2pivot(fileobj)
        else:
            raise UserError(_("Are you sure this file is an XLSX, XLS or ODS file?"))

    def genericxlsx2pivot(self, fileobj):
        wb = openpyxl.load_workbook(fileobj.name, read_only=True)
        sh = wb.active
        res = []
        i = 0
        for row in sh.rows:
            i += 1
            if i == 1 and self.file_with_header:
                continue
            if len(row) < 8:
                continue
            if not [item for item in row if item.value]:
                # skip empty line
                continue
            vals = {
                'date': row[0].value,
                'journal': row[1].value,
                'account': str(row[2].value),
                'partner': row[3].value or False,
                'analytic': row[4].value or False,
                'name': row[5].value,
                'debit': row[6].value,
                'credit': row[7].value,
                'ref': len(row) > 8 and row[8].value or '',
                'reconcile_ref': len(row) > 9 and row[9].value or '',
                'line': i,
                }
            res.append(vals)
        return res

    def genericxls2pivot(self, fileobj):
        wb = xlrd.open_workbook(fileobj.name)
        sh = wb.sheet_by_index(0)
        res = []
        i = 0
        for row_int in range(sh.nrows):
            row = sh.row(row_int)
            i += 1
            if i == 1 and self.file_with_header:
                continue
            if len(row) < 8:
                continue
            if not [item for item in row if item.value]:
                # skip empty line
                continue
            account = row[2].value
            if isinstance(account, float):
                account = str(int(account))
            elif isinstance(account, int):
                account = str(account)
            vals = {
                'date': datetime(*xlrd.xldate_as_tuple(row[0].value, wb.datemode)),
                'journal': row[1].value,
                'account': account,
                'partner': row[3].value or False,
                'analytic': row[4].value or False,
                'name': row[5].value,
                'debit': row[6].value,
                'credit': row[7].value,
                'ref': len(row) > 8 and row[8].value or '',
                'reconcile_ref': len(row) > 9 and row[9].value or '',
                'line': i,
                }
            res.append(vals)
        return res

    def genericods2pivot(self, fileobj):
        if rows is None:
            raise UserError(_(
                "To import ods files, you must install the rows lib from "
                "https://github.com/turicas/rows"))
        if rows.__version__ <= '0.4.1':
            raise UserError(_(
                "Python lib 'rows' 0.4.1 is buggy. "
                "You should checkout the code from https://github.com/turicas/rows"))
        fields_ods = OrderedDict([
            ('date', rows.fields.DateField),
            ('journal', rows.fields.TextField),
            ('account', rows.fields.TextField),
            ('partner', rows.fields.TextField),
            ('analytic', rows.fields.TextField),
            ('name', rows.fields.TextField),
            ('debit', rows.fields.FloatField),
            ('credit', rows.fields.FloatField),
            ('ref', rows.fields.TextField),
            ('reconcile_ref', rows.fields.TextField),
            ])

        sh = rows.import_from_ods(fileobj.name, fields=fields_ods, skip_header=False)

        res = []
        i = 0
        for row in sh:
            # the rows lib automatically skips empty lines
            i += 1
            if i == 1 and self.file_with_header:
                continue
            vals = {
                'date': row.date,
                'journal': row.journal,
                'account': row.account,
                'partner': row.partner,
                'analytic': row.analytic,
                'name': row.name,
                'debit': row.debit,
                'credit': row.credit,
                'ref': row.ref,
                'reconcile_ref': row.reconcile_ref,
                'line': i,
                }
            res.append(vals)
        return res

    def nibelis2pivot(self, fileobj):
        fieldnames = [
            'trasha', 'trashb', 'journal', 'trashd', 'trashe',
            'trashf', 'trashg', 'date', 'trashi', 'trashj', 'trashk',
            'trashl', 'trashm', 'trashn', 'account', 'trashp',
            'trashq', 'amount', 'trashs', 'sign', 'trashu',
            'trashv', 'name',
            'trashx', 'trashy', 'trashz', 'trashaa', 'trashab',
            'trashac', 'trashad', 'trashae', 'analytic']
        res = []
        with open(fileobj.name, newline='', encoding='latin1') as f:
            reader = csv.DictReader(
                f,
                fieldnames=fieldnames,
                delimiter=';',
                quoting=csv.QUOTE_MINIMAL)
            i = 0
            for l in reader:
                i += 1
                if i == 1:
                    continue
                amount = float(l['amount'].replace(',', '.'))
                credit = l['sign'] == 'C' and amount or False
                debit = l['sign'] == 'D' and amount or False
                vals = {
                    'journal': l['journal'],
                    'account': l['account'],
                    'credit': credit,
                    'debit': debit,
                    'date': datetime.strptime(l['date'], '%y%m%d'),
                    'name': l['name'],
                    'line': i,
                }
                if l.get('analytic'):
                    vals['analytic'] = l['analytic']
                res.append(vals)
        return res

    def quadra2pivot(self, file_bytes):
        i = 0
        res = []
        file_str = file_bytes.decode(self.file_encoding)
        for l in file_str.split('\n'):
            i += 1
            if len(l) < 54:
                continue
            if l[0] == 'M' and l[41] in ('C', 'D'):
                amount_cents = int(l[42:55])
                amount = amount_cents / 100.0
                vals = {
                    'journal': l[9:11],
                    'account': l[1:9],
                    'credit': l[41] == 'C' and amount or False,
                    'debit': l[41] == 'D' and amount or False,
                    'date': datetime.strptime(l[14:20], '%d%m%y'),
                    'name': l[21:41],
                    'line': i,
                }
                res.append(vals)
        return res

    def payfit2pivot(self, fileobj):
        # Columns in Payfit exported CSV :
        # JournalCode
        # JournalLib
        # EcritureDate
        # CompteNum
        # CompteLib
        # Debit
        # Credit
        # AxeLib
        # AxeReference
        res = []
        with open(fileobj.name, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=";")
            i = 0
            for l in reader:
                i += 1
                vals = {
                    "journal": l.get("JournalCode", ""),
                    "account": l["CompteNum"],
                    "name": l["CompteLib"],
                    "credit": float(l["Credit"] or 0.0),
                    "debit": float(l["Debit"] or 0.0),
                    "analytic": l.get("AxeReference", ""),
                    "date": datetime.strptime(l["EcritureDate"], "%d/%m/%Y"),
                    "line": i,
                }
                res.append(vals)
        return res

    def _prepare_partner_speeddict(self, company_id):
        speeddict = {}
        partner_sr = self.env['res.partner'].search_read(
            [
                '|',
                ('company_id', '=', company_id),
                ('company_id', '=', False),
                ('ref', '!=', False),
                ('parent_id', '=', False),
            ],
            ['ref'])
        for l in partner_sr:
            speeddict[l['ref'].upper()] = l['id']
        return speeddict

    def _prepare_speeddict(self, company_id):
        speeddict = {
            "partner": self._prepare_partner_speeddict(company_id),
            "journal": {},
            "account": {},
            "analytic": {},
            }
        acc_sr = self.env['account.account'].search_read([
            ('company_id', '=', company_id),
            ('deprecated', '=', False)], ['code'])
        for l in acc_sr:
            speeddict['account'][l['code'].upper()] = l['id']
        aacc_sr = self.env['account.analytic.account'].search_read(
            [('company_id', 'in', (company_id, False)), ('code', '!=', False)],
            ['code'])
        for l in aacc_sr:
            speeddict['analytic'][l['code'].upper()] = l['id']
        journal_sr = self.env['account.journal'].search_read([
            ('company_id', '=', company_id)], ['code'])
        for l in journal_sr:
            speeddict['journal'][l['code'].upper()] = l['id']
        return speeddict

    def create_moves_from_pivot(self, pivot, post=False):
        logger.debug('Final pivot: %s', pivot)
        amo = self.env['account.move']
        company_id = self.company_id.id
        speeddict = self._prepare_speeddict(company_id)
        key2label = {
            'journal': _('journal codes'),
            'account': _('account codes'),
            'partner': _('partner reference'),
            'analytic': _('analytic codes'),
            }
        errors = {'other': []}
        for key in key2label.keys():
            errors[key] = {}
        # MATCHES + CHECKS
        for l in pivot:
            assert l.get('line') and isinstance(l.get('line'), int), \
                'missing line number'
            if l['account'] in speeddict['account']:
                l['account_id'] = speeddict['account'][l['account']]
            if not l.get('account_id'):
                # Match when import = 61100000 and Odoo has 611000
                acc_code_tmp = l['account']
                while acc_code_tmp and acc_code_tmp[-1] == '0':
                    acc_code_tmp = acc_code_tmp[:-1]
                    if acc_code_tmp and acc_code_tmp in speeddict['account']:
                        l['account_id'] = speeddict['account'][acc_code_tmp]
                        break
            if not l.get('account_id'):
                # Match when import = 611000 and Odoo has 611000XX
                for code, account_id in speeddict['account'].items():
                    if code.startswith(l['account']):
                        logger.warning(
                            "Approximate match: import account %s has been matched "
                            "with Odoo account %s" % (l['account'], code))
                        l['account_id'] = account_id
                        break
            if not l.get('account_id'):
                errors['account'].setdefault(l['account'], []).append(l['line'])
            if l.get('partner'):
                if l['partner'] in speeddict['partner']:
                    l['partner_id'] = speeddict['partner'][l['partner']]
                else:
                    errors['partner'].setdefault(l['partner'], []).append(l['line'])
            if l.get('analytic'):
                l['analytic_distribution'] = {}
                for ana_entry in l['analytic'].split('|'):
                    ana_entry = ana_entry.strip()
                    if ana_entry:
                        ana_entry_split = ana_entry.split(':')
                        if len(ana_entry_split) == 1:
                            ana_account_code = ana_entry_split[0].strip()
                            ana_pct = 100
                        elif len(ana_entry_split) > 1:
                            ana_account_code = ':'.join(ana_entry_split[:-1]).strip()
                            ana_pct_str = ana_entry_split[-1]
                            ana_pct_str_ready = ana_pct_str.replace(',', '.')
                            try:
                                ana_pct = float(ana_pct_str_ready)
                            except Exception:
                                errors['other'].append("Line %d: wrong analytic percentage: '%s' is not a number." % (l['line'], ana_pct_str))
                                ana_pct = 1
                            if ana_pct < 0 or ana_pct > 100:
                                errors['other'].append("Line %d: wrong analytic percentage: '%s' is not between 0 and 100." % (l['line'], ana_pct_str))
                        if ana_account_code in speeddict['analytic']:
                            l['analytic_distribution'][speeddict['analytic'][ana_account_code]] = ana_pct
                        else:
                            errors['analytic'].setdefault(ana_account_code, []).append(l['line'])

            if l['journal'] in speeddict['journal']:
                l['journal_id'] = speeddict['journal'][l['journal']]
            else:
                errors['journal'].setdefault(l['journal'], []).append(l['line'])
            if not l.get('date'):
                errors['other'].append(_(
                    'Line %d: missing date.') % l['line'])
            else:
                if not isinstance(l.get('date'), datelib):
                    try:
                        l['date'] = datetime.strptime(l['date'], '%Y-%m-%d')
                    except Exception:
                        errors['other'].append(_(
                            'Line %d: bad date format %s') % (l['line'], l['date']))
            if not isinstance(l.get('credit'), (float, int)):
                errors['other'].append(_(
                    'Line %d: bad value for credit (%s).')
                    % (l['line'], l['credit']))
            if not isinstance(l.get('debit'), (float, int)):
                errors['other'].append(_(
                    'Line %d: bad value for debit (%s).')
                    % (l['line'], l['debit']))
            # test that they don't have both a value
        # LIST OF ERRORS
        msg = ''
        for key, label in key2label.items():
            if errors[key]:
                msg += _("List of %s that don't exist in Odoo:\n%s\n\n") % (
                    label,
                    '\n'.join([
                        '- %s : line(s) %s' % (code, ', '.join([str(i) for i in lines]))
                        for (code, lines) in errors[key].items()]))
        if errors['other']:
            msg += _('List of misc errors:\n%s') % (
                '\n'.join(['- %s' % e for e in errors['other']]))
        if msg:
            raise UserError(msg)
        # EXTRACT MOVES
        skip_null_lines = self.skip_null_lines
        split_move_method = self.split_move_method
        moves = []
        cur_journal_id = False
        cur_move_name = False
        cur_date = False
        cur_balance = 0.0
        comp_cur = self.company_id.currency_id
        seq = self.env['ir.sequence'].next_by_code('account.move.import')
        cur_move = {}
        for l in pivot:
            if (
                    skip_null_lines and
                    comp_cur.is_zero(l['credit']) and
                    comp_cur.is_zero(l['debit'])):
                logger.info('Skip line %d which has debit=credit=0', l['line'])
                continue
            move_name = l.get('move_name')
            if split_move_method == 'move_name':
                if not move_name:
                    errors['other'].append(_(
                        'Line %d: missing journal entry number.') % l['line'])
                same_move = [cur_move_name == move_name]
            elif split_move_method == 'balanced':
                same_move = [
                    cur_journal_id == l['journal_id'],
                    not comp_cur.is_zero(cur_balance)]
                if not self.date_by_move_line:
                    same_move.append(cur_date == l['date'])
            else:
                raise UserError(_("Wrong Move Split Method."))
            if all(same_move):  # append to current move
                cur_move['line_ids'].append((0, 0, self._prepare_move_line(l, seq)))
            else:  # new move
                if cur_move:
                    if len(cur_move['line_ids']) <= 1:
                        raise UserError(_(
                            "Journal entry on line %d only has 1 line.\n\n"
                            "Debug data: %s") % (l['line'], cur_move['line_ids']))
                    moves.append(cur_move)
                cur_move = self._prepare_move(l)
                cur_move['line_ids'] = [(0, 0, self._prepare_move_line(l, seq))]
                cur_date = l['date']
                cur_move_name = move_name
                cur_journal_id = l['journal_id']
                cur_balance = 0.0
            cur_balance += l['credit'] - l['debit']
        if cur_move:
            moves.append(cur_move)
        if not comp_cur.is_zero(cur_balance):
            raise UserError(_(
                "The journal entry that ends on the last line is not "
                "balanced (balance is %s).") % cur_balance)
        rmoves = self.env['account.move']
        for move in moves:
            rmoves += amo.create(move)
        logger.info(
            'Account moves IDs %s created via file import' % rmoves.ids)
        if post:
            rmoves.action_post()
        return rmoves

    def _prepare_move(self, pivot_line):
        vals = {
            'journal_id': pivot_line['journal_id'],
            'ref': pivot_line.get('ref'),
            'date': pivot_line['date'],
            }
        if pivot_line.get('move_name') and not self.keep_odoo_move_name:
            vals['name'] = pivot_line['move_name']
        return vals

    def _prepare_move_line(self, pivot_line, sequence):
        vals = {
            'credit': pivot_line['credit'],
            'debit': pivot_line['debit'],
            'name': pivot_line['name'],
            'partner_id': pivot_line.get('partner_id'),
            'account_id': pivot_line['account_id'],
            'analytic_distribution': pivot_line.get('analytic_distribution'),
            'import_reconcile': pivot_line.get('reconcile_ref'),
            'import_external_id': '%s-%s' % (sequence, pivot_line.get('line')),
            }
        return vals

    def reconcile_move_lines(self, moves):
        comp_cur = self.company_id.currency_id
        logger.info('Start to reconcile imported moves')
        lines = self.env['account.move.line'].search([
            ('move_id', 'in', moves.ids),
            ('import_reconcile', '!=', False),
            ])
        torec = {}  # key = reconcile mark, value = movelines_recordset
        for line in lines:
            if line.import_reconcile in torec:
                torec[line.import_reconcile] |= line
            else:
                torec[line.import_reconcile] = line
        for rec_ref, lines_to_rec in torec.items():
            if len(lines_to_rec) < 2:
                logger.warning(
                    "Skip reconcile of ref '%s' because "
                    "this ref is only on 1 move line", rec_ref)
                continue
            total = 0.0
            accounts = {}
            partners = {}
            for line in lines_to_rec:
                total += line.credit
                total -= line.debit
                accounts[line.account_id] = True
                partners[line.partner_id.id or False] = True
            if not comp_cur.is_zero(total):
                logger.warning(
                    "Skip reconcile of ref '%s' because the lines with "
                    "this ref are not balanced (%s)", rec_ref, total)
                continue
            if len(accounts) > 1:
                logger.warning(
                    "Skip reconcile of ref '%s' because the lines with "
                    "this ref have different accounts (%s)",
                    rec_ref, ', '.join([acc.code for acc in accounts.keys()]))
                continue
            if not list(accounts)[0].reconcile:
                logger.warning(
                    "Skip reconcile of ref '%s' because the account '%s' "
                    "is not configured with 'Allow Reconciliation'",
                    rec_ref, list(accounts)[0].display_name)
                continue
            if len(partners) > 1:
                logger.warning(
                    "Skip reconcile of ref '%s' because the lines with "
                    "this ref have different partners (IDs %s)",
                    rec_ref, ', '.join(partners.keys()))
                continue
            lines_to_rec.reconcile()
        logger.info('Reconcile imported moves finished')
