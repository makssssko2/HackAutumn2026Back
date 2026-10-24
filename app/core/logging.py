import logging
import re


class PublicInvoiceLogFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            if isinstance(args[2], str):
                args[2] = re.sub(r'(/api/v1/public/invoices/|/invoice/)\S*', r'\1[redacted]', args[2])
            record.args = tuple(args)
        return True


def protect_public_invoice_logs():
    logging.getLogger('uvicorn.access').addFilter(PublicInvoiceLogFilter())
