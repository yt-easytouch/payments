# Copyright (c) 2024, Frappe Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
import uuid
import time
import json
import requests


class WaafiPay(Document):
	pass
class WaafiPay(Document):
    def hosted_session_process_hormuud_by_docname(self, payer_info):
        merchant_id = self.merchant_id
        if not merchant_id:
            return {'status': False,'error_type': 'alert', 'code': '3020',
                'message': 'Waafi Merchant Not Availability'
            }

        data = {
            "schemaVersion": "1.0",
            "requestId": str(uuid.uuid4()),
            "timestamp": str(int(time.time())),
            "channelName": "WEB",
            "serviceName": "API_PURCHASE",
            "serviceParams": {
                "merchantUid": merchant_id,
                "apiUserId": self.user_id,
                "apiKey": self.merchant_key,
                "paymentMethod": payer_info.get('paymentMethod', "MWALLET_ACCOUNT"),
                "payerInfo": {
                    "accountNo": payer_info.get('accountNo')
                },
                "transactionInfo": {
                    "referenceId": payer_info.get('referenceId'),
                    "invoiceId": str(payer_info.get('invoiceId')),
                    "amount": str(payer_info.get('amount_paid')),
                    "currency": "USD",
                    "description": payer_info.get('description', '') + " via " + payer_info.get('paymentType', '')
                }
            }
        }

        for optional_key in ['accountPwd', 'accountExpDate', 'accountHolder']:
            if optional_key in payer_info:
                data["serviceParams"]["payerInfo"][optional_key] = payer_info[optional_key]

        json_data = json.dumps(data)

        if merchant_id == 'test':
            response = {
                # ... same as before ...
            }
        else:
            url = self.endpoint
            headers = {
                "Content-Type": "text/plain",
                "Content-Length": str(len(json_data))
            }
            try:
                resp = requests.post(url, data=json_data, headers=headers, timeout=30)
                resp.raise_for_status()
                response = resp.json()
            except requests.RequestException as e:
                return {
                    'status': False,
                    'error_type': 'alert',
                    'code': '3020',
                    'message': f"Payment API request failed: {e}"
                }

        if response.get('responseCode') == "2001":
            params = response.get('params', {})
            narration = (
                f"{payer_info.get('accountNo', '')} | Amount: {params.get('txAmount')} | "
                f"merchantCharges: {params.get('merchantCharges')} | transactionId: {params.get('transactionId')}"
            )
            return {
                'account_id': self.account_id_hormuud,
                'Narration': narration,
                'status': True
            }
        else:
            msg = response.get('params', {}).get('description', 'Unknown error from payment gateway')
            return {
                'status': False,
                'error_type': 'alert',
                'code': '3020',
                'message': msg
            }
