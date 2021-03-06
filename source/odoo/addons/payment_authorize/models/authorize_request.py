# -*- coding: utf-8 -*-
import requests
from lxml import etree, objectify
from StringIO import StringIO
from xml.etree import ElementTree as ET
from uuid import uuid4

from odoo import _
from odoo.exceptions import ValidationError, UserError
from odoo import _

XMLNS = 'AnetApi/xml/v1/schema/AnetApiSchema.xsd'


def strip_ns(xml, ns):
    """Strip the provided name from tag names.

    :param str xml: xml document
    :param str ns: namespace to strip

    :rtype: etree._Element
    :return: the parsed xml string with the namespace prefix removed
    """
    it = ET.iterparse(StringIO(xml))
    ns_prefix = '{%s}' % XMLNS
    for _, el in it:
        if el.tag.startswith(ns_prefix):
            el.tag = el.tag[len(ns_prefix):]  # strip all Auth.net namespaces
    return it.root


class AuthorizeAPI():
    """Authorize.net Gateway API integration.

    This class allows contacting the Authorize.net API with simple operation
    requests. It implements a *very limited* subset of the complete API
    (http://developer.authorize.net/api/reference); namely:
        - Customer Profile/Payment Profile creation
        - Transaction authorization/capture/voiding
    """
    def __init__(self, acquirer):
        """Initiate the environment with the acquirer data.

        :param record acquirer: payment.acquirer account that will be contacted
        """
        if acquirer.environment == 'test':
            self.url = 'https://apitest.authorize.net/xml/v1/request.api'
        else:
            self.url = 'https://api.authorize.net/xml/v1/request.api'
        self.name = acquirer.authorize_login
        self.transaction_key = acquirer.authorize_transaction_key

    def _authorize_request(self, data):
        """Encode, send and process the request to the Authorize.net API.

        Encodes the xml data and process the response. Note that only a basic
        processing is done at this level (namespace cleanup, basic error management).

        :param etree._Element data: etree data to process
        """
        data = etree.tostring(data, xml_declaration=True, encoding='utf-8')
        r = requests.post(self.url, data=data, headers={'Content-Type': 'text/xml'})
        r.raise_for_status()
        response = strip_ns(r.content, XMLNS)
        if response.find('messages/resultCode').text == 'Error':
            messages = [m.text for m in response.findall('messages/message/text')]
            raise ValidationError(_('Authorize.net Error Message(s):\n %s') % '\n'.join(messages))
        return response

    def _base_tree(self, requestType):
        """Create a basic tree containing authentication information.

        Create a etree Element of type requestType and appends the Authorize.net
        credentials (they are always required).
        :param str requestType: the type of request to send to Authorize.net
                                See http://developer.authorize.net/api/reference
                                for available types.
        :return: basic etree Element of the requested type
                               containing credentials information
        :rtype: etree._Element
        """
        root = etree.Element(requestType, xmlns=XMLNS)
        auth = etree.SubElement(root, "merchantAuthentication")
        etree.SubElement(auth, "name").text = self.name
        etree.SubElement(auth, "transactionKey").text = self.transaction_key
        return root

    # Customer profiles
    def create_customer_profile(self, partner, cardnumber, expiration_date, card_code):
        """Create a payment and customer profile in the Authorize.net backend.

        Creates a customer profile for the partner/credit card combination and links
        a corresponding payment profile to it. Note that a single partner in the Odoo
        database can have multiple customer profiles in Authorize.net (i.e. a customer
        profile is created for every res.partner/payment.token couple).

        :param record partner: the res.partner record of the customer
        :param str cardnumber: cardnumber in string format (numbers only, no separator)
        :param str expiration_date: expiration date in 'YYYY-MM' string format
        :param str card_code: three- or four-digit verification number

        :return: a dict containing the profile_id and payment_profile_id of the
                 newly created customer profile and payment profile
        :rtype: dict
        """
        root = self._base_tree('createCustomerProfileRequest')
        profile = etree.SubElement(root, "profile")
        etree.SubElement(profile, "merchantCustomerId").text = 'ODOO-%s-%s' % (partner.id, uuid4().hex[:8])
        etree.SubElement(profile, "email").text = partner.email
        payment_profile = etree.SubElement(profile, "paymentProfiles")
        etree.SubElement(payment_profile, "customerType").text = 'business' if partner.is_company else 'individual'
        billTo = etree.SubElement(payment_profile, "billTo")
        etree.SubElement(billTo, "address").text = (partner.street or '' + (partner.street2 if partner.street2 else '')) or None
        etree.SubElement(billTo, "city").text = partner.city
        etree.SubElement(billTo, "state").text = partner.state_id.name or None
        etree.SubElement(billTo, "zip").text = partner.zip
        etree.SubElement(billTo, "country").text = partner.country_id.name or None
        payment = etree.SubElement(payment_profile, "payment")
        creditCard = etree.SubElement(payment, "creditCard")
        etree.SubElement(creditCard, "cardNumber").text = cardnumber
        etree.SubElement(creditCard, "expirationDate").text = expiration_date
        etree.SubElement(creditCard, "cardCode").text = card_code
        etree.SubElement(root, "validationMode").text = 'liveMode'
        response = self._authorize_request(root)
        res = dict()
        res['profile_id'] = response.find('customerProfileId').text
        res['payment_profile_id'] = response.find('customerPaymentProfileIdList/numericString').text
        return res

    def create_customer_profile_from_tx(self, partner, transaction_id):
        """Create an Auth.net payment/customer profile from an existing transaction.

        Creates a customer profile for the partner/credit card combination and links
        a corresponding payment profile to it. Note that a single partner in the Odoo
        database can have multiple customer profiles in Authorize.net (i.e. a customer
        profile is created for every res.partner/payment.token couple).

        Note that this function makes 2 calls to the authorize api, since we need to
        obtain a partial cardnumber to generate a meaningful payment.token name.

        :param record partner: the res.partner record of the customer
        :param str transaction_id: id of the authorized transaction in the
                                   Authorize.net backend

        :return: a dict containing the profile_id and payment_profile_id of the
                 newly created customer profile and payment profile as well as the
                 last digits of the card number
        :rtype: dict
        """
        root = self._base_tree('createCustomerProfileFromTransactionRequest')
        etree.SubElement(root, "transId").text = transaction_id
        customer = etree.SubElement(root, "customer")
        etree.SubElement(customer, "merchantCustomerId").text = 'ODOO-%s-%s' % (partner.id, uuid4().hex[:8])
        etree.SubElement(customer, "email").text = partner.email or ''
        response = self._authorize_request(root)
        res = dict()
        res['profile_id'] = response.find('customerProfileId').text
        res['payment_profile_id'] = response.find('customerPaymentProfileIdList/numericString').text
        root_profile = self._base_tree('getCustomerPaymentProfileRequest')
        etree.SubElement(root_profile, "customerProfileId").text = res['profile_id']
        etree.SubElement(root_profile, "customerPaymentProfileId").text = res['payment_profile_id']
        response_profile = self._authorize_request(root_profile)
        res['name'] = response_profile.find('paymentProfile/payment/creditCard/cardNumber').text
        return res

    # Transaction management
    def auth_and_capture(self, token, amount, reference):
        """Authorize and capture a payment for the given amount.

        Authorize and immediately capture a payment for the given payment.token
        record for the specified amount with reference as communication.

        :param record token: the payment.token record that must be charged
        :param str amount: transaction amount (up to 15 digits with decimal point)
        :param str reference: used as "invoiceNumber" in the Authorize.net backend

        :return: a dict containing the response code, transaction id and transaction type
        :rtype: dict
        """
        root = self._base_tree('createTransactionRequest')
        tx = etree.SubElement(root, "transactionRequest")
        etree.SubElement(tx, "transactionType").text = "authCaptureTransaction"
        etree.SubElement(tx, "amount").text = str(amount)
        profile = etree.SubElement(tx, "profile")
        etree.SubElement(profile, "customerProfileId").text = token.authorize_profile
        payment_profile = etree.SubElement(profile, "paymentProfile")
        etree.SubElement(payment_profile, "paymentProfileId").text = token.acquirer_ref
        order = etree.SubElement(tx, "order")
        etree.SubElement(order, "invoiceNumber").text = reference
        response = self._authorize_request(root)
        res = dict()
        res['x_response_code'] = response.find('transactionResponse/responseCode').text
        res['x_trans_id'] = response.find('transactionResponse/transId').text
        res['x_type'] = 'auth_capture'
        return res

    def authorize(self, token, amount, reference):
        """Authorize a payment for the given amount.

        Authorize (without capture) a payment for the given payment.token
        record for the specified amount with reference as communication.

        :param record token: the payment.token record that must be charged
        :param str amount: transaction amount (up to 15 digits with decimal point)
        :param str reference: used as "invoiceNumber" in the Authorize.net backend

        :return: a dict containing the response code, transaction id and transaction type
        :rtype: dict
        """
        root = self._base_tree('createTransactionRequest')
        tx = etree.SubElement(root, "transactionRequest")
        etree.SubElement(tx, "transactionType").text = "authOnlyTransaction"
        etree.SubElement(tx, "amount").text = str(amount)
        profile = etree.SubElement(tx, "profile")
        etree.SubElement(profile, "customerProfileId").text = token.authorize_profile
        payment_profile = etree.SubElement(profile, "paymentProfile")
        etree.SubElement(payment_profile, "paymentProfileId").text = token.acquirer_ref
        order = etree.SubElement(tx, "order")
        etree.SubElement(order, "invoiceNumber").text = reference
        response = self._authorize_request(root)
        res = dict()
        res['x_response_code'] = response.find('transactionResponse/responseCode').text
        res['x_trans_id'] = response.find('transactionResponse/transId').text
        res['x_type'] = 'auth_only'
        return res

    def capture(self, transaction_id, amount):
        """Capture a previously authorized payment for the given amount.

        Capture a previsouly authorized payment. Note that the amount is required
        even though we do not support partial capture.

        :param str transaction_id: id of the authorized transaction in the
                                   Authorize.net backend
        :param str amount: transaction amount (up to 15 digits with decimal point)

        :return: a dict containing the response code, transaction id and transaction type
        :rtype: dict
        """
        root = self._base_tree('createTransactionRequest')
        tx = etree.SubElement(root, "transactionRequest")
        etree.SubElement(tx, "transactionType").text = "priorAuthCaptureTransaction"
        etree.SubElement(tx, "amount").text = str(amount)
        etree.SubElement(tx, "refTransId").text = transaction_id
        response = self._authorize_request(root)
        res = dict()
        res['x_response_code'] = response.find('transactionResponse/responseCode').text
        res['x_trans_id'] = response.find('transactionResponse/transId').text
        res['x_type'] = 'prior_auth_capture'
        return res

    def void(self, transaction_id):
        """Void a previously authorized payment.

        :param str transaction_id: the id of the authorized transaction in the
                                   Authorize.net backend

        :return: a dict containing the response code, transaction id and transaction type
        :rtype: dict
        """
        root = self._base_tree('createTransactionRequest')
        tx = etree.SubElement(root, "transactionRequest")
        etree.SubElement(tx, "transactionType").text = "voidTransaction"
        etree.SubElement(tx, "refTransId").text = transaction_id
        response = self._authorize_request(root)
        res = dict()
        res['x_response_code'] = response.find('transactionResponse/responseCode').text
        res['x_trans_id'] = response.find('transactionResponse/transId').text
        res['x_type'] = 'void'
        return res

    # Test
    def test_authenticate(self):
        """Test Authorize.net communication with a simple credentials check.

        :return: True if authentication was successful, else False (or throws an error)
        :rtype: bool
        """
        test_auth = self._base_tree('authenticateTestRequest')
        response = self._authorize_request(test_auth)
        root = objectify.fromstring(response)
        if root.find('{ns}messages/{ns}resultCode'.format(ns='{%s}' % XMLNS)) == 'Ok':
            return True
        return False
