"""Native Norvale engine. One durable event sequence for notebook and batch."""
import sys, json, hashlib, time, os, random, math, fcntl
from pathlib import Path
from functools import cached_property
from html import escape
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from imaplib import IMAP4
from smtplib import SMTPRecipientsRefused, SMTPResponseException, SMTPServerDisconnected
from datetime import datetime, timedelta, timezone
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from norvale.environment.stack import Stack
from norvale.environment import compose as compose_mod
from norvale.loader.erp_client import ErpClient, ErpError
from walkthrough.engine import EnterpriseSimV4, ROLES

RUN = Path(__file__).parent/'run'  # preserved fixed-scenario evidence
ACTIVE_RUN = Path(__file__).parent/'recurring-run'
ERP_ROLES = {'BUYER':['Purchase User'],'SCM_MGR':['Purchase User','Purchase Manager'],'SALES':['Sales User'],'WAREHOUSE':['Stock User'],'PLANNER':['Manufacturing User','Stock User'],'PRODUCTION':['Manufacturing User','Stock User'],'QA':['Quality Manager','Stock User'],'FINANCE':['Accounts User','Accounts Manager'],'MAINTENANCE':['Maintenance User'],'IT_ADMIN':['System Manager','Item Manager'],'CFO':['Accounts Manager','Purchase Manager']}

def observable(value):
    if isinstance(value, dict):return {k:observable(v) for k,v in value.items() if k!='custom_norvale_operation'}
    if isinstance(value, list):return [observable(v) for v in value]
    return value

def save(path, value):
    path=Path(path); value=observable(value) if 'observable' in path.parts else value; path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_suffix('.tmp'); tmp.write_text(json.dumps(value, indent=2, default=str)); tmp.replace(path)

def project(run):
    return 'norvale-generation-dev' if Path(run).resolve()==RUN.resolve() else 'nvg-'+hashlib.sha256(str(Path(run).resolve()).encode()).hexdigest()[:12]

def provision(run=ACTIVE_RUN):
    stack=Stack(run); stack.project=project(run)
    original=compose_mod.build
    def build(*a, **kw):
        c=original(*a, **kw)
        c['services']['db']['command'].append('--innodb-fatal-semaphore-wait-threshold=4294967295')
        return c
    compose_mod.build=build
    ids=[{'id':r['role_id'], 'mail':r['email'].replace('.example','.test'), 'chat':r['role_id'].lower()} for r in ROLES]
    port = int(os.environ["NORVALE_ERP_PORT"]) if os.environ.get("NORVALE_ERP_PORT") else None
    try:stack.up(ids, datetime(2027,1,1,tzinfo=timezone.utc), frontend_port=port)
    finally:compose_mod.build=original
    return stack


class Engine:
    company='Norvale Consumer Care BV'
    abbr='NCC'
    def __init__(self, run=ACTIVE_RUN, *, company_authority=None):
        self.run = Path(run)
        path = self.run/'config.json'
        self.config = json.loads(path.read_text()) if path.exists() else None
        if self.config:
            self.validate_config(self.config)
        checkpoint = self.run/'state.json'
        if checkpoint.exists():
            from native.company_world import check_binding
            check_binding(self.config or {}, json.loads(checkpoint.read_text()))
        self.additions = None
        self.company_erp = self.make_company_erp(company_authority)
        self.run=Path(run); self.stack=Stack(self.run); self.stack.project=project(self.run)
        self.eps=self.stack.endpoints()
        self.catalog=EnterpriseSimV4(seed=self.config['seed'] if self.config else 42, days=30, mode='dry-run')
        self.state=json.loads((self.run/'state.json').read_text()) if (self.run/'state.json').exists() else {'docs':{},'completed':[], 'physical':{}, 'decisions':{}, 'day':0}
        self.docs=self.state['docs']; self.api={}; self.event='setup'; self.day=self.state['day']
        self.wh={k:f'{v} - NCC' for k,v in [('rm','Raw Materials'),('fg','Finished Goods'),('wip','Work In Progress'),('hold','Quarantine')]}
        self.roles={role:list(permissions) for role,permissions in ERP_ROLES.items()}
        secpath=self.run/'env/role-tokens.json'
        if secpath.exists():
            self.tokens=json.loads(secpath.read_text()); self.connect()
        else:self.tokens={}
        self.log('controller_started',source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())

    @cached_property
    def admin(self):
        token_path = self.run / 'env/controller-token.json'
        if token_path.exists():
            token = json.loads(token_path.read_text())['token']
            return ErpClient(self.eps['erp'], self.eps['site'], token)
        client = ErpClient(self.eps['erp'], self.eps['site'])
        client.login('Administrator', 'admin')
        secret = client.call('frappe.core.doctype.user.user.generate_keys', {'user': 'Administrator'})['api_secret']
        client.token = client.get('User', 'Administrator')['api_key'] + ':' + secret
        save(token_path, {'token': client.token})
        token_path.chmod(0o600)
        return client

    def make_company_erp(self, authority):
        factory = (self.config or {}).get('company_authority')
        checkpoint = self.run/'state.json'
        if checkpoint.exists():
            state = json.loads(checkpoint.read_text())
            integrated = 'company_erp' in state
            if integrated and not factory:
                raise ValueError('Existing company lot integration requires company_authority')
            if 'seed' in state['completed'] and factory and not integrated:
                raise ValueError('Company lot integration requires a fresh recurring run')
        if not factory:
            if authority is not None:
                raise ValueError('Enable company_authority in a fresh recurring configuration')
            return None
        if authority is None:
            from importlib import import_module
            module, name = factory.split(':', 1)
            authority = getattr(import_module(module), name)(self.run)
        from native.company_erp import CompanyERP
        return CompanyERP(self, authority)

    def recover_company_doc(self, key, doctype, role):
        if key not in self.docs:
            found = self.api[role].list(doctype, {'custom_norvale_operation': hashlib.sha256(key.encode()).hexdigest()})
            if found:
                self.docs[key] = {'doctype': doctype, 'name': found[0]['name']}
                self.persist()
        return self.get(key, role) if key in self.docs else None

    def connect(self):
        self.api={r:ErpClient(self.eps['erp'],self.eps['site'],t) for r,t in self.tokens.items()}

    def log(self,kind,**data):
        self.run.mkdir(parents=True, exist_ok=True)
        trace=self.run/'trace.jsonl';trace.touch(mode=0o600, exist_ok=True);trace.chmod(0o600)
        with trace.open('a') as f:f.write(json.dumps({'event':self.event,'day':self.day,'kind':kind,**data},default=str)+'\n')

    def persist(self):
        if (getattr(self, 'config', None) or {}).get('world') and 'world_binding' not in self.state:
            from native.company_world import binding
            self.state['world_binding'] = binding(self.config)
        self.state['day']=self.day;save(self.run/'state.json',self.state)

    def write(self,key,dt,data,role=None,submit=False,*,before_submit=None):
        api=self.api[role] if role else self.admin
        mutated=False
        if key in self.docs:
            saved=self.docs[key]; assert saved['doctype']==dt
            doc=api.get(dt,saved['name'])
        else:
            if role and self.admin.list('Custom Field',{'dt':dt,'fieldname':'custom_norvale_operation'}):
                existing=api.list(dt,{'custom_norvale_operation':hashlib.sha256(key.encode()).hexdigest()})
                if existing:
                    self.docs[key]={'doctype':dt,'name':existing[0]['name']};self.persist()
                    return self.write(key,dt,data,role,submit,before_submit=before_submit)
                data={**data,'custom_norvale_operation':hashlib.sha256(key.encode()).hexdigest()}
            self.log('request',role=role or 'setup administrator',method='POST',doctype=dt,body=data)
            try:doc=api.create(dt,data);mutated=True
            except Exception as e:self.log('rejected',error=str(e));raise
            self.docs[key]={'doctype':dt,'name':doc['name']};self.persist()
            self.log('created',role=role,doc=doc)
        if submit and doc['docstatus']==0:
            if before_submit:
                before_submit(doc)
            self.log('request',role=role,method='PUT',doctype=dt,name=doc['name'],body={'docstatus':1})
            doc=api.update(dt,doc['name'],{'docstatus':1});mutated=True
        actual=api.get(dt,doc['name']) if mutated else doc
        if submit:assert actual['docstatus']==1
        self.log('readback',role=role,doc=actual)
        return actual

    def get(self,key,role=None):
        d=self.docs[key];return (self.api[role] if role else self.admin).get(d['doctype'],d['name'])

    def mapped(self,role,method,existing=None,**args):
        # Recover before asking ERP to map a source that may already be fully consumed.
        if existing:
            key,dt=existing
            if key not in self.docs:
                found=self.api[role].list(dt,{'custom_norvale_operation':hashlib.sha256(key.encode()).hexdigest()})
                if found:self.docs[key]={'doctype':dt,'name':found[0]['name']};self.persist()
            if key in self.docs:return self.get(key,role)
        self.log('mapper_request',role=role,method=method,args=args)
        doc=self.api[role].call(method,args)
        self.log('mapper_response',role=role,response=doc);return doc

    def seed(self):
        if not self.admin.list('Company',{'name':self.company}):
            self.admin.call('frappe.desk.page.setup_wizard.setup_wizard.setup_complete',{'args':{'language':'English','full_name':'ERP Admin','email':'it@norvale.test','password':'Admin!2026x','chart_of_accounts':'Standard','setup_demo':0,'company_name':self.company,'company_abbr':self.abbr,'country':'Netherlands','currency':'EUR','timezone':'UTC','fy_start_date':'2027-01-01','fy_end_date':'2027-12-31'}})
            self.admin.login('Administrator','admin')
        if not self.admin.list('Fiscal Year',{'name':'2027'}):self.admin.create('Fiscal Year',{'year':'2027','year_start_date':'2027-01-01','year_end_date':'2027-12-31'})
        for role,perms in self.roles.items():
            email=role.lower()+'@norvale.test'
            if not self.admin.list('User',{'name':email}):self.admin.create('User',{'email':email,'first_name':role,'enabled':1,'send_welcome_email':0,'roles':[{'role':r} for r in perms]})
            if role not in self.tokens:
                secret=self.admin.call('frappe.core.doctype.user.user.generate_keys',{'user':email})['api_secret'];self.tokens[role]=self.admin.get('User',email)['api_key']+':'+secret
                save(self.run/'env/role-tokens.json',self.tokens);(self.run/'env/role-tokens.json').chmod(0o600)
        self.connect()
        for k,name in self.wh.items():
            if not self.admin.list('Warehouse',{'name':name}):self.admin.create('Warehouse',{'warehouse_name':name.split(' - ')[0],'company':self.company})
        self.admin.update('Stock Settings','Stock Settings',{'allow_negative_stock':0,'allow_to_make_quality_inspection_after_purchase_or_delivery':1})
        for c in self.catalog.accounts.values():self.write('customer:'+c['account_id'],'Customer',{'customer_name':c['name'],'customer_type':'Company','customer_group':'Commercial','territory':'All Territories','credit_limits':[{'company':self.company,'credit_limit':c['credit_limit_eur']}]})
        for k,v in self.catalog.vendors.items():self.write('vendor:'+k,'Supplier',{'supplier_name':v['name'],'supplier_group':'All Supplier Groups','supplier_type':'Company'})
        lot_flow = getattr(self, 'company_erp', None)
        batch_settings = {'has_batch_no': 1, 'create_new_batch': 0} if lot_flow else {}
        if lot_flow:
            from native.stock_precision import provision_stock_precision
            provision_stock_precision(self.admin)
            self.configure()
            if not self.admin.list('Custom Field', {'dt': 'Work Order', 'fieldname': 'custom_norvale_definition'}):
                self.admin.create('Custom Field', {
                    'dt': 'Work Order', 'fieldname': 'custom_norvale_definition',
                    'label': 'Approved PLM definition', 'fieldtype': 'Long Text', 'read_only': 1,
                })
        for code,m in self.catalog.materials.items():
            self.write('item:'+code,'Item',{'item_code':code,'item_name':m['name'],'item_group':'Raw Material','stock_uom':'Kg' if m['uom']=='KG' else 'Nos','is_stock_item':1,'valuation_rate':m['std_cost'],**batch_settings})
        # Explicit water closes V4's otherwise implicit recipe mass balance.
        self.write('item:WATER','Item',{'item_code':'WATER','item_name':'Process water','item_group':'Raw Material','stock_uom':'Kg','is_stock_item':1,'valuation_rate':0.001,**batch_settings})
        for s in self.catalog.skus:
            code=s['sku_id']; self.write('item:'+code,'Item',{'item_code':code,'item_name':s['name'],'item_group':'Products','stock_uom':'Nos','is_stock_item':1,**batch_settings})
            ingredients=[{'item_code':k,'qty':q/1000,'rate':self.catalog.materials[k]['std_cost']} for k,q in s['bom'].items()]
            mass=sum(q/1000 for k,q in s['bom'].items() if self.catalog.materials[k]['uom']=='KG')
            ingredients.append({'item_code':'WATER','qty':s['pack_size_kg']-mass,'rate':0.001})
            self.write('bom:'+code,'BOM',{'item':code,'quantity':1,'company':self.company,'currency':'EUR','items':ingredients},submit=True)
        if lot_flow or 'opening' not in self.docs:
            items=[{'item_code':k,'qty':10000 if m['uom']=='KG' else 100000,'t_warehouse':self.wh['rm'],'basic_rate':m['std_cost']} for k,m in self.catalog.materials.items()]
            items.append({'item_code':'WATER','qty':100000,'t_warehouse':self.wh['rm'],'basic_rate':0.001})
            if self.config:
                use=self.usage()
                for row in items:row['qty']=max(1,math.ceil(use.get(row['item_code'],0)*self.config['opening_stock_days']))
                items[-1]['qty']=100000  # process water is supplied by an unconstrained utility tank
            doc = {'stock_entry_type':'Material Receipt','company':self.company,'items':items}
            if lot_flow:
                for row in items:
                    row['t_warehouse'] = self.wh['hold']
                actual = lot_flow.prepare_receipt('opening', 'Stock Entry', doc, 'WAREHOUSE')
                lot_flow.receive(actual, 'opening', 'WAREHOUSE')
            else:
                self.write('opening','Stock Entry',doc,'WAREHOUSE',True)
        self.persist()

    def decision(self,key,role,evidence,question):
        if key in self.state['decisions']:return self.state['decisions'][key]
        prompt={'role':role,'as_of':self.date(),'question':question,'evidence':evidence,'instruction':'Return JSON with action, reason, message. Do not assume unseen facts. Prior decisions may be mistaken: reassess using current native evidence and explicit operating policies; do not perpetuate unsupported concerns. Action may be approve, defer, reject, investigate. Approval is not implementation. You may propose standing_policy with effective_date, review_date, nonnegative numeric approval_limit and reorder_target, nonempty exact suppliers and items lists. Only SCM_MGR/CFO may authorize it. Ground all values in observed evidence; omit if insufficient.'}
        keyenv=os.environ.get('OPENAI_API_KEY') or os.environ.get('OPENROUTER_API_KEY')
        model_base=None
        if os.environ.get('NORVALE_OFFLINE')=='1':keyenv=None
        if keyenv:
            import urllib.request
            base=model_base or os.environ.get('OPENAI_BASE_URL','https://openrouter.ai/api/v1' if os.environ.get('OPENROUTER_API_KEY') else 'https://api.openai.com/v1')
            model=os.environ.get('NORVALE_MODEL','openai/gpt-4.1-mini' if 'openrouter' in base else 'gpt-4.1-mini')
            payload={'model':model,'messages':[{'role':'user','content':json.dumps(prompt)}],'response_format':{'type':'json_object'},'max_tokens':1600}
            self.log('model_request',model=model,input=prompt)
            req=urllib.request.Request(base+'/chat/completions',json.dumps(payload).encode(),headers={'Authorization':'Bearer '+keyenv,'Content-Type':'application/json'})
            from router import provider_json
            raw=provider_json('Model',req,lambda attempt:self.log('model_http_attempt',attempt=attempt))
            self.log('model_response',response=raw)
            if raw['choices'][0].get('finish_reason')!='stop':raise RuntimeError('Incomplete model response; raw response retained for diagnosis')
            answer=json.loads(raw['choices'][0]['message']['content']);mode='model'
        else:raise RuntimeError('LLM judgment requires credentials; no approval fallback')
        if answer.get('action') not in ['approve','defer','reject','investigate']:raise ValueError('Invalid decision action')
        if answer.get('standing_policy') is not None:
            try:self.adopt_policy(role,answer['standing_policy'],evidence,key)
            except (ValueError,KeyError,TypeError) as exc:
                answer['policy_rejected']=str(exc);self.log('policy_rejected',role=role,error=str(exc),proposal=answer.pop('standing_policy'))
        result={'mode':mode,'input':prompt,'output':answer};self.state['decisions'][key]=result;self.persist();self.log('decision',**result);return result

    def date(self):return (datetime(2027,1,1)+timedelta(days=self.day)).date().isoformat()

    def configure(self):
        if getattr(self, 'company_erp', None):
            evidence = dict(dt='Delivery Note', fieldname='custom_norvale_quality_releases')
            if not self.admin.list('Custom Field', evidence):
                self.admin.create('Custom Field', {**evidence, 'label':'Batch quality releases',
                                  'fieldtype':'Long Text', 'no_copy':1, 'read_only':1})
        for dt in ['Stock Ledger Entry','Bin']:
            for role in ['Purchase User','Purchase Manager']:
                if not self.admin.list('Custom DocPerm',{'parent':dt,'role':role,'read':1}):
                    result=self.stack.bench('execute','frappe.permissions.add_permission','--kwargs',json.dumps({'doctype':dt,'role':role,'ptype':'read'}))
                    if result.returncode:raise RuntimeError(result.stderr[-500:])
        perms=self.admin.list('Custom DocPerm',{'parent':'Account','role':'Stock User'},['name','select'])
        if not perms:
            r=self.stack.bench('execute','frappe.permissions.add_permission','--kwargs',json.dumps({'doctype':'Account','role':'Stock User','ptype':'select'}))
            if r.returncode:raise RuntimeError(r.stderr[-500:])
        for dt in ['Stock Entry','Sales Order','Purchase Order','Purchase Receipt','Purchase Invoice','Work Order','Quality Inspection','Delivery Note','Sales Invoice','Payment Entry','ToDo','Comment','Journal Entry','Opportunity','Note','Supplier Quotation']:
            if not self.admin.list('Custom Field',{'dt':dt,'fieldname':'custom_norvale_operation'}):
                self.admin.create('Custom Field',{'dt':dt,'fieldname':'custom_norvale_operation','label':'Norvale operation','fieldtype':'Data','unique':1,'no_copy':1,'read_only':1})

    def workflow_setup(self):
        for name in ['Pending Approval','Approved','Rejected']:
            if not self.admin.list('Workflow State',{'name':name}):self.admin.create('Workflow State',{'workflow_state_name':name})
        for name in ['Approve','Reject']:
            if not self.admin.list('Workflow Action Master',{'name':name}):self.admin.create('Workflow Action Master',{'workflow_action_name':name})
        self.write('po-workflow','Workflow',{'workflow_name':'Norvale purchasing approval','document_type':'Purchase Order','is_active':1,'workflow_state_field':'workflow_state','states':[{'state':'Pending Approval','doc_status':'0','allow_edit':'Purchase User'},{'state':'Approved','doc_status':'1','allow_edit':'Purchase Manager'},{'state':'Rejected','doc_status':'0','allow_edit':'Purchase Manager'}],'transitions':[{'state':'Pending Approval','action':'Approve','next_state':'Approved','allowed':'Purchase Manager','allow_self_approval':0},{'state':'Pending Approval','action':'Reject','next_state':'Rejected','allowed':'Purchase Manager','allow_self_approval':0}]})

    def mail(self,key,sender,recipient,subject,body):
        from native.company_erp import MailReadbackError
        from norvale.agents.comms import Comms
        roster={r['role_id']:r for r in ROLES};sec=json.loads((self.run/'env/secrets.json').read_text())
        def client(role):
            role={'BUYER':'BUYER_CHEM','SCM_MGR':'SC_MGR','WAREHOUSE':'WH_CLERK','MAINTENANCE':'MAINT_LEAD'}.get(role,role)
            r=roster[role];return Comms({'mail':r['email'].replace('.example','.test'),'chat':role.lower()},sec[role],self.eps)
        reference = hashlib.sha256(key.encode()).hexdigest()
        marker = f'[Ref {reference[:16]}] '
        origin, receiver = client(sender), client(recipient)
        message_id = f'<norvale-{reference}@{origin.addr.split("@")[1]}>'
        ledger = self.state.setdefault('mail_operations', {})
        sent_day = ledger.get(key, {}).get('sent_day', self.date())
        sent_at = datetime.fromisoformat(sent_day).replace(tzinfo=timezone.utc)
        expected = EmailMessage()
        expected.set_content(body)
        intent = {'from': origin.addr, 'to': receiver.addr, 'subject': marker + subject,
                  'body': expected.get_content()}
        fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()

        def record(status, confirmed_id=None):
            if status == 'rejected':
                del ledger[key]
            else:
                ledger[key] = {'status': status, 'fingerprint': fingerprint,
                               'message_id': confirmed_id or message_id, 'sent_day': sent_day}
            self.persist()

        if key in ledger and ledger[key]['fingerprint'] != fingerprint:
            raise MailReadbackError('Mail operation intent changed on retry')

        def delivered():
            known_id = ledger.get(key, {}).get('message_id', message_id)
            candidates = [row for row in receiver.inbox(limit=sys.maxsize)
                          if row['subject'].startswith(marker) or row['message_id'] == known_id]
            if not candidates:
                return False
            if len(candidates) != 1:
                raise MailReadbackError('Duplicate mail operation reference')
            message = receiver.read_mail(candidates[0]['uid'], body_limit=None)
            actual = {k: message[k] for k in intent}
            actual['body'] = actual['body'].replace('\r\n', '\n').replace('\r', '\n')
            expected_id = ledger.get(key, candidates[0])['message_id']
            try:
                delivered_at = parsedate_to_datetime(message.get('date') or '')
            except (TypeError, ValueError, OverflowError):
                delivered_at = None
            if (delivered_at != sent_at or actual != intent or message['cc'] or message['attachments']
                    or message['content_type'] != 'text/plain'
                    or message['message_id'] != expected_id
                    or message['message_id'] != candidates[0]['message_id']):
                raise MailReadbackError('Mail delivery readback differs from intended message')
            record('confirmed', message['message_id'])
            self.log('native_mail_readback',sender=sender,recipient=recipient,message=message)
            return True

        if delivered():
            return
        error = None
        if key not in ledger:
            try:
                origin.send_mail([receiver.addr], intent['subject'], body,
                                 sent_at,
                                 message_id=message_id, on_status=record)
            except OSError as exc:
                if key not in ledger:
                    raise
                error = exc
        try:
            self.stack.settle()
        except TimeoutError as exc:
            error = exc
        if not delivered():
            raise TimeoutError(f'Mail delivery pending ({ledger[key]["status"]}) for {key}') from error

    def purchase(self):
        po=self.write('po','Purchase Order',{'company':self.company,'supplier':self.get('vendor:BRENNER')['name'],'schedule_date':'2027-01-04','items':[{'item_code':'SLES','qty':500,'rate':1.42,'schedule_date':'2027-01-04','warehouse':self.wh['hold']}]},'BUYER')
        self.mail('po-request','BUYER','SCM_MGR','Purchase approval requested',f"Please review {po['name']}; 500 kg SLES at EUR 1.42/kg, due January 4.")

    def approve_purchase(self):
        po=self.get('po','SCM_MGR')
        if po['docstatus']==1:return
        answer=self.decision('purchase','SCM_MGR',[{k:po.get(k) for k in ['name','supplier','grand_total','schedule_date','items']}],'Approve this replenishment purchase or defer/reject/investigate?')
        if answer['output']['action']=='approve':
            self.log('request',role='SCM_MGR',method='POST',path='/api/method/frappe.model.workflow.apply_workflow',body={'doc':po,'action':'Approve'})
            doc=self.api['SCM_MGR'].call('frappe.model.workflow.apply_workflow',{'doc':json.dumps(po),'action':'Approve'})
            assert self.get('po','SCM_MGR')['docstatus']==1;self.log('approved',doc=doc)
        else:self.log('pending',reason=answer['output']);return
        self.mail('po-approved','SCM_MGR','BUYER','Purchase approved',answer['output']['message'])

    def arrive(self):
        if self.get('po')['docstatus']!=1:return
        self.state['physical']['SLES shipment']={'arrived':self.date(),'qty':500,'receipt_booked':False,'inspection':'not yet sampled'};self.persist()
        self.mail('arrival','WAREHOUSE','BUYER','Truck arrived; receipt pending','500 kg SLES at receiving dock. Receipt booking waits for paperwork tomorrow.')

    def receipt(self):
        if 'SLES shipment' not in self.state['physical']:return
        doc=self.mapped('WAREHOUSE','erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt',source_name=self.get('po')['name'])
        for r in doc['items']:r.update(warehouse=self.wh['hold'])
        self.write('receipt','Purchase Receipt',doc,'WAREHOUSE',True)
        self.state['physical']['SLES shipment']['receipt_booked']=True;self.persist()

    def quality(self,key,item,qty,reference,reading=70):
        ref=self.get(reference)
        doc=self.write('qc:'+key,'Quality Inspection',{'inspection_type':'Incoming' if ref['doctype']=='Purchase Receipt' else 'In Process','reference_type':ref['doctype'],'reference_name':ref['name'],'item_code':item,'sample_size':1,'inspected_by':'qa@norvale.test','manual_inspection':1,'status':'Accepted' if 66<=reading<=72 else 'Rejected','readings':[{'specification':'Active percentage' if item=='SLES' else 'Release assay','reading_1':str(reading),'numeric':1,'min_value':66,'max_value':72}]},'QA',True)
        if doc['status']=='Accepted':
            self.write('release:'+key,'Stock Entry',{'company':self.company,'stock_entry_type':'Material Transfer','items':[{'item_code':item,'qty':qty,'s_warehouse':self.wh['hold'],'t_warehouse':self.wh['rm'] if item=='SLES' else self.wh['fg']}]},'QA',True)
        return doc

    def supplier_invoice(self):
        if 'receipt' not in self.docs:return
        doc=self.mapped('FINANCE','erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice',source_name=self.get('receipt')['name'])
        self.write('supplier-invoice','Purchase Invoice',doc,'FINANCE',True)

    def order(self,index):
        s=self.catalog.skus[index];customer=list(self.catalog.accounts.values())[index]
        self.write('order:'+str(index),'Sales Order',{'company':self.company,'customer':self.get('customer:'+customer['account_id'])['name'],'delivery_date':'2027-01-15','items':[{'item_code':s['sku_id'],'qty':240,'rate':s['unit_price'],'delivery_date':'2027-01-15','warehouse':self.wh['fg']}]},'SALES',True)

    def manufacture(self,index):
        if not self.state['physical'].get('Filler F1',{}).get('available',True):
            raise RuntimeError('Filler unavailable: production must wait for technician completion')
        s=self.catalog.skus[index];key=str(index)
        wo=self.write('work:'+key,'Work Order',{'production_item':s['sku_id'],'bom_no':self.get('bom:'+s['sku_id'])['name'],'qty':240,'company':self.company,'use_multi_level_bom':0,'skip_transfer':1,'source_warehouse':self.wh['rm'],'wip_warehouse':self.wh['wip'],'fg_warehouse':self.wh['hold'],'planned_start_date':self.date()+' 08:00:00','sales_order':self.get('order:'+key)['name']},'PLANNER',True)
        doc=self.mapped('PRODUCTION','erpnext.manufacturing.doctype.work_order.work_order.make_stock_entry',work_order_id=wo['name'],purpose='Manufacture',qty=240)
        for r in doc['items']:
            if r.get('is_finished_item'):r['t_warehouse']=self.wh['hold']
            else:r['s_warehouse']=self.wh['rm']
        self.write('manufacture:'+key,'Stock Entry',doc,'PRODUCTION',True)
        self.state['physical']['production:'+key]={'completed':self.date(),'qty':240,'quality':'pending'};self.persist()

    def ship(self,index):
        key=str(index)
        if 'release:product:'+key not in self.docs:
            self.log('blocked',reason='No accepted finished-goods release');return
        doc=self.mapped('WAREHOUSE','erpnext.selling.doctype.sales_order.sales_order.make_delivery_note',source_name=self.get('order:'+key)['name'])
        self.write('delivery:'+key,'Delivery Note',doc,'WAREHOUSE',True)
        self.state['physical']['dispatch:'+key]={'departed':self.date(),'qty':240};self.persist()

    def invoice(self,index):
        key=str(index)
        if 'delivery:'+key not in self.docs:return
        doc=self.mapped('FINANCE','erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice',source_name=self.get('delivery:'+key)['name'])
        self.write('invoice:'+key,'Sales Invoice',doc,'FINANCE',True)

    def pay(self,key,ref):
        if ref not in self.docs:return
        invoice=self.get(ref)
        doc=self.mapped('FINANCE','erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry',existing=(key,'Payment Entry'),dt=invoice['doctype'],dn=invoice['name'],bank_account='Cash - NCC')
        doc.update(reference_no=key,reference_date=self.date())
        self.write(key,'Payment Entry',doc,'FINANCE',True)
        actual=self.get(ref);assert abs(actual['outstanding_amount'])<0.01
        self.log('settled_invoice',doc=actual)

    def maintenance_request(self):
        self.write('maintenance','ToDo',{'description':'Filler F1 vibration observed; inspect before next production release. Completion requires technician report.','allocated_to':'maintenance@norvale.test','date':self.date(),'status':'Open'},'MAINTENANCE')
        self.state['physical']['Filler F1']={'available':False,'fault_observed':self.date()};self.persist()
        self.mail('maintenance','PLANT_MGR','COO','Filler inspection pending','Filler F1 is unavailable. Maintenance owns inspection; no completion has been reported.')

    def maintenance_complete(self):
        doc=self.get('maintenance','MAINTENANCE')
        self.api['MAINTENANCE'].update('ToDo',doc['name'],{'status':'Closed','description':doc['description']+'\nTechnician: loose mount tightened; restart check passed on '+self.date()})
        self.log('maintenance_readback',doc=self.get('maintenance','MAINTENANCE'))
        self.state['physical']['Filler F1']['available']=True;self.persist()

    def change_request(self):
        self.write('change','ToDo',{'description':'Request: raise SLES reorder level to 600 kg in Raw Materials. Buyer requests SCM approval; IT implements after approval.','allocated_to':'scm_mgr@norvale.test','status':'Open'},'BUYER')

    def approve_change(self):
        request=self.get('change','SCM_MGR')
        evidence=[{'request':request['name'],'description':request['description']}, {'purchase':self.get('po','SCM_MGR')['name']}]
        decision=self.decision('master-change','SCM_MGR',evidence,'Approve the proposed reorder level change?')
        if decision['output']['action']=='approve':
            self.write('change-approval','ToDo',{'reference_type':'ToDo','reference_name':request['name'],'allocated_to':'it_admin@norvale.test','description':'APPROVED: SLES reorder level 600 kg in Raw Materials; assigned IT for implementation.'},'SCM_MGR')
        self.mail('change-decision','SCM_MGR','IT_ADMIN','Master data decision',json.dumps(decision['output']))

    def implement_change(self,approval_key='change-approval'):
        if approval_key not in self.docs:raise ValueError('No native approval record exists')
        approval=self.get(approval_key,'IT_ADMIN');request=self.get('change','IT_ADMIN')
        if approval['owner']!='scm_mgr@norvale.test' or approval['reference_name']!=request['name'] or not approval['description'].startswith('APPROVED: SLES reorder level 600 kg'):
            raise ValueError('Approval author, target or approved change does not match')
        doc=self.api['IT_ADMIN'].update('Item','SLES',{'reorder_levels':[{'warehouse':self.wh['rm'],'warehouse_reorder_level':600,'warehouse_reorder_qty':500,'material_request_type':'Purchase'}]})
        self.api['IT_ADMIN'].update('ToDo',request['name'],{'status':'Closed'})
        actual=self.api['IT_ADMIN'].get('Item','SLES')
        assert actual['reorder_levels'][0]['warehouse_reorder_level']==600
        self.api['IT_ADMIN'].update('ToDo',approval['name'],{'status':'Closed'})
        self.log('implemented_change_readback',doc=actual)

    def finance_costs(self, opening=False):
        entries=([('Cash - NCC',50000,0),('Capital Stock - NCC',0,50000)] if opening else
                 [('Salary - NCC',2500,0),('Utility Expenses - NCC',300,0),('Cash - NCC',0,2800)])
        self.write('funding' if opening else 'monthly-costs','Journal Entry',{'company':self.company,'posting_date':self.date(),'voucher_type':'Journal Entry','user_remark':'Development opening capital' if opening else 'Representative period aggregated wages and utilities; no employee payroll module','accounts':[{'account':a,'debit_in_account_currency':d,'credit_in_account_currency':c} for a,d,c in entries]},'FINANCE',True)

    def report(self):
        gl=self.api['FINANCE'].list('GL Entry',{'company':self.company,'posting_date':['<=',self.date()]},['name','posting_date','account','debit','credit','voucher_type','voucher_no'],limit=10000)
        totals={}
        for r in gl:totals[r['account']]=round(totals.get(r['account'],0)+r['debit']-r['credit'],2)
        save(self.run/'native-ledger.json',gl)
        self.write('report','Note',{'title':'January operating and financial review','public':0,'content':'<pre>'+json.dumps({'as_of':self.date(),'native_ledger_rows':len(gl),'account_balances':totals,'pending_work':[r for r in self.admin.list('ToDo',{'status':'Open'},['name','description'])]},indent=2)+'</pre>'},'CFO')
        self.mail('monthly-report','CFO','CEO','January operating review','Native ledger and outstanding work are ready in ERPNext Note '+self.get('report')['name']+'. This is a review request, not a completed meeting.')

    def management_review(self):
        ledger=self.api['CFO'].list('GL Entry',{'company':self.company,'posting_date':['<=',self.date()]},['account','debit','credit','voucher_type','voucher_no'],limit=10000)
        totals={}
        for r in ledger:totals[r['account']]=round(totals.get(r['account'],0)+r['debit']-r['credit'],2)
        evidence={'native_account_balances':totals,'report':self.get('report','CFO')['name'],'report_note':'Opening stock was seeded through a stock receipt. Separate opening setup effects from operating performance. Wages are an aggregate prototype accrual/payment.'}
        outcome=self.decision('finance-review','CFO',evidence,'Review these account balances. Approve the report, investigate discrepancies by assigning Finance, defer for more evidence, or reject it. Do not infer unobserved plant facts.')
        if outcome['output']['action'] in ('investigate','reject','defer'):
            self.write('finance-followup','ToDo',{'description':outcome['output']['reason'],'allocated_to':'finance@norvale.test','date':'2027-02-03','status':'Open'},'CFO')
        self.mail('finance-review','CFO','CEO','CFO review outcome',outcome['output']['message'])

    # Recurring operation: each phase plans from current native records once, then
    # durably executes that plan. Retry never samples new demand or doubles a booking.
    def usage(self):
        use={k:0.0 for k in self.catalog.materials};use['WATER']=0.0
        for s in self.catalog.skus:
            units=s['daily_units']*self.config['demand_scale']*self.config['orders_per_day']/len(self.catalog.skus)
            mass=0
            for code,q in s['bom'].items():
                use[code]+=q/1000*units
                if self.catalog.materials[code]['uom']=='KG':mass+=q/1000
            use['WATER']+=(s['pack_size_kg']-mass)*units
        return use

    def dated(self,day):return (datetime(2027,1,1)+timedelta(days=day)).date().isoformat()

    def observe(self,role,dt,filters,fields):
        as_of = self.date()
        date_field = {
            'GL Entry': 'posting_date', 'Stock Ledger Entry': 'posting_date',
            'Purchase Receipt': 'posting_date', 'Sales Order': 'transaction_date',
            'Purchase Order': 'transaction_date',
        }.get(dt)
        if date_field:
            cutoff = ['<=', as_of]
            if isinstance(filters, dict):
                bound = filters.get(date_field)
                if date_field not in filters:
                    filters = {**filters, date_field: cutoff}
                elif isinstance(bound, (list, tuple)) and bound[0] == '<=':
                    filters = {**filters, date_field: ['<=', min(as_of, bound[1])]}
                else:
                    filters = [[field, *value] if isinstance(value, (list, tuple))
                               else [field, '=', value] for field, value in filters.items()]
                    filters.append([date_field, *cutoff])
            else:
                filters = [*(filters or ()), [date_field, *cutoff]]
        rows=self.api[role].list(dt,filters,fields,limit=10000)
        self.log('observation',role=role,doctype=dt,as_of=as_of,rows=rows)
        return rows

    def inventory(self,role):
        rows=self.observe(role,'Bin',{'warehouse':['in',list(self.wh.values())]},['item_code','warehouse','actual_qty'])
        return {(r['item_code'],r['warehouse']):float(r['actual_qty']) for r in rows}

    def plan(self,make):
        plans=self.state.setdefault('plans',{})
        if self.event not in plans:
            plans[self.event]=make();self.persist();self.log('planned',plan=plans[self.event])
        return plans[self.event]

    def effective_policies(self,role):
        return [{k:v for k,v in p.items() if k!='observed_evidence'} for p in self.state.get('standing_policies',[]) if (p['role']==role or (role=='SCM_MGR' and p['role']=='CFO')) and p['effective_date']<=self.date()<p.get('review_date',self.date())]

    def adopt_policy(self,role,p,evidence,key):
        if role not in ('SCM_MGR','CFO'):raise ValueError('Unauthorized policy author')
        for field in ('approval_limit','reorder_target'):
            if type(p.get(field)) not in (int,float) or not math.isfinite(p[field]) or p[field]<0:raise ValueError('Invalid numeric policy')
        for field in ('suppliers','items'):
            if not isinstance(p.get(field),list) or not p[field] or any(not isinstance(v,str) or not v for v in p[field]):raise ValueError('Invalid policy scope')
        start=datetime.fromisoformat(p['effective_date']).date().isoformat();end=datetime.fromisoformat(p['review_date']).date().isoformat()
        if not self.date()<=start<end:raise ValueError('Invalid policy dates')
        self.write('policy:'+key,'Note',{'title':'Purchasing policy '+hashlib.sha256(key.encode()).hexdigest()[:8]+' effective '+start,'public':1,'content':json.dumps({'authorized_by':role,'effective_date':start,'review_date':end,'policy':p})},role)
        self.state.setdefault('standing_policies',[]).append({**p,'role':role,'effective_date':start,'review_date':end,'source_decision':key})
        self.persist();self.log('policy_authorized',role=role,policy=p,evidence=evidence)

    def choose(self,key,role,evidence,question,action='approve',model=False):
        if key in self.state['decisions']:return self.state['decisions'][key]['output']
        if (getattr(self, 'config', None) or {}).get('company_operating') and os.environ.get('NORVALE_OFFLINE') == '1':
            from integrations.company_run.operating import offline_decision
            return offline_decision(self, key, role, evidence, question)
        from router import route
        policies=self.effective_policies(role)
        history=[d for d in self.state['decisions'].values() if d['input']['role']==role and d['mode']!='provider-unavailable']
        def relevant(d):
            previous=d['input'].get('evidence',{});previous=previous.get('observations',previous)
            return evidence.get('supplier') is not None and previous.get('supplier')==evidence['supplier']
        cases=[{'as_of':d['input']['as_of'],'mode':d['mode'],'output':{'action':d['output']['action'],'reason':d['output'].get('reason','')[:240]}} for d in history if relevant(d)][-6:]
        matches=[p for p in policies if role=='SCM_MGR' and type(evidence.get('grand_total')) in (int,float) and 0<=evidence['grand_total']<=p['approval_limit'] and evidence.get('supplier') in p['suppliers'] and evidence.get('items') and all(r.get('item_code') in p['items'] and r.get('qty',0)>0 and r.get('rate',-1)>=0 for r in evidence['items'])]
        policy=matches[-1] if matches else None
        def comparable_evidence(value):
            normalized = dict(value)
            for field in ('supplier_receipts', 'other_open_orders', 'coverage'):
                if field in normalized:
                    normalized[field] = sorted(normalized[field], key=lambda row: json.dumps(row, sort_keys=True))
            return normalized

        current = comparable_evidence(evidence)
        def comparable(d):
            return comparable_evidence(d['input'].get('evidence', {})) == current
        cached=policy and any(d.get('policy')==policy and d.get('classification') is False and comparable(d) for d in history)
        route_evidence={**evidence,'policy_check':{'applicable_policy':False,'authorization':'No matching effective policy; classify this as a new judgment case unless an explicit authorized procedure is supplied.'}}
        route_cases=cases
        if policy:
            route_evidence={**evidence,'policy_check':{'policy_authorizes':'SCM_MGR pre-approves POs from these suppliers for these items with grand_total <= approval_limit between effective_date and review_date','supplier_in_scope':True,'all_items_in_scope':True,'grand_total':evidence['grand_total'],'approval_limit':policy['approval_limit'],'within_dates':True}}
            route_cases=[c for c in cases if c['as_of']>=policy['effective_date']]
        routed=({'judgment':False,'cached':True} if cached else route(route_evidence,route_cases,[policy] if policy else [],lambda attempt:self.log('jev_http_attempt',attempt=attempt)))
        self.log('jev_route',role=role,evidence=evidence,policy=policy,**routed)
        if routed['judgment']:
            return self.decision(key,role,{'observations':evidence,'prior_cases':cases,'effective_policies':policies},question)['output']
        if not policy:
            result={'mode':'pending-policy','input':{'role':role,'as_of':self.date(),'evidence':evidence},'output':{'action':'defer','reason':'Router classified routine, but no authorized rule can execute this case. Request an applicable policy or revised classification.','message':'Pending authorized procedure; no automatic approval.'}}
            self.state['decisions'][key]=result;self.persist();self.log('decision',**result);return result['output']
        result={'mode':'standing-policy','input':{'role':role,'as_of':self.date(),'evidence':evidence},'policy':policy,'classification':False,'output':{'action':'approve','reason':'Within authorized policy scope and review date','message':'Purchase approved under standing policy.'}}
        self.state['decisions'][key]=result;self.persist();self.log('decision',**result)
        return result['output']

    def observed_usage(self):
        filters = {'posting_date':['<=',self.date()],'actual_qty':['<',0],'warehouse':self.wh['rm']}
        lot_flow = getattr(self, 'company_erp', None)
        if lot_flow:
            # A quarantine transfer is containment, not material use.
            filters['voucher_type'] = 'Stock Entry'
            filters['voucher_no'] = ['in', [p['target_id'] for p in lot_flow.state['completions'].values()
                                            if p.get('target_id')] or ['']]
        rows=self.observe('BUYER','Stock Ledger Entry',filters,['item_code','actual_qty','posting_date'])
        use={}
        for row in rows:use[row['item_code']]=use.get(row['item_code'],0)-float(row['actual_qty'])/max(1,self.day+1)
        forecast_key='forecast:'+str(self.day)
        if forecast_key in self.docs:
            delivered=self.get(forecast_key,'BUYER');self.log('observation',role='BUYER',doctype='ToDo',as_of=self.date(),rows=[delivered])
            for code,value in json.loads(delivered['description'])['daily_material_forecast'].items():use[code]=max(use.get(code,0),value)
            return use
        forecast={}
        orders=self.observe('PLANNER','Sales Order',{'docstatus':1,'status':['not in',['Closed','Completed','Cancelled']]},['name'])
        for order in orders:
            doc=self.api['PLANNER'].get('Sales Order',order['name'])
            for row in doc['items']:
                remaining=max(0,float(row['qty'])-float(row.get('delivered_qty',0)))
                if lot_flow:
                    planned = lot_flow.attempt('forecast-definition:' + row['item_code'], 'PLANNER',
                                               lambda: lot_flow.forecast_components(row['item_code']))
                    if planned is not None:
                        for code, per in planned.items():
                            forecast[code] = forecast.get(code, 0) + remaining * per / 7
                        continue
                boms=self.observe('PLANNER','BOM',{'item':row['item_code'],'is_active':1,'is_default':1,'docstatus':1},['name'])
                for bom in boms:
                    recipe=self.api['PLANNER'].get('BOM',bom['name']);self.log('forecast_evidence',role='PLANNER',order=doc,bom=recipe)
                    for component in recipe['items']:
                        code=component['item_code'];forecast[code]=forecast.get(code,0)+remaining*float(component['qty'])/float(recipe['quantity'])/7
        record=self.write('forecast:'+str(self.day),'ToDo',{'description':json.dumps({'as_of':self.date(),'daily_material_forecast':forecast,'basis':'Open submitted orders and active native BOMs; seven-day planning window'}),'allocated_to':'buyer@norvale.test'},'PLANNER')
        delivered=self.api['BUYER'].get('ToDo',record['name']);self.log('observation',role='BUYER',doctype='ToDo',as_of=self.date(),rows=[delivered])
        for code,value in json.loads(delivered['description'])['daily_material_forecast'].items():use[code]=max(use.get(code,0),value)
        return use

    def recurring_setup(self):
        self.configure();self.workflow_setup();self.finance_costs(True)
        from systems_v2 import identities
        aliases={'SC_MGR':'SCM_MGR','BUYER_CHEM':'BUYER','MAINT_LEAD':'MAINTENANCE','QA_TECH':'QA','ACCT_EXEC':'SALES','SHIFT_OP':'PRODUCTION','WH_CLERK':'WAREHOUSE'}
        roster=[{'role':aliases.get(r['role_id'],r['role_id']),'user':aliases.get(r['role_id'],r['role_id']).lower()+'@norvale.test','name':r['person'],'mail':r['email'].replace('.example','.test'),'manager':aliases.get(r.get('reports_to'),r.get('reports_to')) if r.get('reports_to')!='BOARD' else None} for r in ROLES if r['role_id']!='BOARD']
        roster.append({'role':'FINANCE','user':'finance@norvale.test','name':'Finance Operations','manager':'CFO'})
        self.state['identities']=identities(self,roster)
        self.write('mandate:2027','Note',{'title':'Operating mandates effective 2027-01-01','public':1,'content':json.dumps({'effective_date':'2027-01-01','review_date':'2027-05-01','SCM_MGR':'Approve or reject supplier purchases and item reorder changes using observed evidence; issue scoped purchasing policies.','CFO':'Review finances and revise purchasing policies; no configured spending ceiling.','BUYER':'Observe materials, request purchases; cannot approve own purchase or book stock.','IT_ADMIN':'Implement separately approved item changes after approval date.','PRODUCTION':'Execute authorized operations within available stock and shift capacity.'})})
        self.state.setdefault('ops',{'orders':{},'purchases':{},'production':{},'invoices':{},'maintenance':{},'changes':{},'meter':0,'serviced_meter':0,'shortage_days':{}})
        self.persist()
        self.seed_opening_orders()

    def seed_opening_orders(self):
        for index, order in enumerate(self.config.get('opening_orders', []), 1):
            key = f'opening-order-{index}'
            sku = next(s for s in self.catalog.skus if s['sku_id'] == order['item'])
            customer = self.get('customer:' + order['customer'])['name']
            due = self.dated(order['due_day'])
            line = dict(item_code=order['item'], qty=order['quantity'], rate=sku['unit_price'],
                        delivery_date=due, warehouse=self.wh['fg'])
            doc = self.write(key, 'Sales Order', dict(company=self.company, customer=customer,
                             transaction_date=self.date(), delivery_date=due, items=[line]), 'SALES', True)
            row, = doc['items']
            if (doc['docstatus'] != 1 or doc['customer'] != customer or doc['company'] != self.company
                    or row['warehouse'] != line['warehouse'] or float(row.get('delivered_qty') or 0) != 0
                    or row['item_code'] != line['item_code']
                    or row['qty'] != line['qty'] or row['delivery_date'] != due or row['rate'] != line['rate']):
                raise ValueError('Opening Sales Order readback differs from scenario')
            self.state['ops']['orders'][key] = dict(key=key, item=order['item'], qty=line['qty'],
                                                    rate=line['rate'], customer=order['customer'], day=self.day)
            self.persist()

    def regime(self):
        from regimes import factors
        value=factors(self.config['seed'],self.day)
        self.log('private_regime',factors=value)
        return value

    def demand(self):
        def build():
            if datetime.fromisoformat(self.date()).weekday()>=5:return []
            rng=random.Random(self.config['seed']+self.day*7919)
            choices=rng.sample(self.catalog.skus,self.config['orders_per_day'])
            customers=list(self.catalog.accounts.values())
            return [{'key':f'd{self.day:04}-order-{s["sku_id"]}','item':s['sku_id'],'qty':max(12,round(s['daily_units']*self.config['demand_scale']*self.regime()['demand']*rng.uniform(.7,1.3)/12)*12),'rate':s['unit_price'],'customer':rng.choices(customers,[c['weight'] for c in customers])[0]['account_id'],'day':self.day} for s in choices]
        for p in self.plan(build):
            self.write(p['key'],'Sales Order',{'company':self.company,'customer':self.get('customer:'+p['customer'])['name'],'delivery_date':self.dated(self.day+7),'items':[{'item_code':p['item'],'qty':p['qty'],'rate':p['rate'],'delivery_date':self.dated(self.day+7),'warehouse':self.wh['fg']}]},'SALES',True)
            self.state['ops']['orders'].setdefault(p['key'],p);self.persist()

    def admit_production_order(self, system, event_id, cfg, payload, ordered, delivery):
        from native.company_erp import Pending

        key = 'addon-order:' + hashlib.sha256(json.dumps([system, event_id]).encode()).hexdigest()
        line = {'item_code': cfg['item'], 'qty': payload['quantity'], 'rate': payload['rate'],
                'warehouse': self.wh['fg'], 'delivery_date': delivery}
        data = {'company': self.company, 'customer': cfg['customer'], 'po_no': payload['case'],
                'transaction_date': ordered, 'delivery_date': delivery, 'ignore_pricing_rule': 1,
                'items': [line]}

        def verify(doc):
            rows = doc.get('items', [])
            if (any(doc.get(k) != data[k] for k in
                    ('company', 'customer', 'po_no', 'transaction_date', 'delivery_date'))
                    or len(rows) != 1 or any(rows[0].get(k) != v for k, v in line.items())
                    or rows[0].get('conversion_factor') != 1 or not rows[0].get('name')):
                raise Pending('Production Sales Order readback differs from approved request')

        doc = self.write(key, 'Sales Order', data, 'SALES', True, before_submit=verify)
        verify(doc)
        if doc.get('docstatus') != 1:
            raise Pending('Production Sales Order has not been submitted')
        order_day = (datetime.fromisoformat(ordered) - datetime(2027, 1, 1)).days
        order = {'key': key, 'item': cfg['item'], 'qty': payload['quantity'], 'rate': payload['rate'],
                 'customer': cfg['customer'], 'day': order_day, 'production_profile': system}
        if self.state['ops']['orders'].setdefault(key, order) != order:
            raise Pending('Production order conflicts with the existing queue entry')
        self.persist()

    @staticmethod
    def purchase_unavailable(po):
        return (po['docstatus'] == 2 or po.get('workflow_state') == 'Rejected'
                or po.get('status') in ('Closed', 'Cancelled', 'Completed', 'On Hold'))

    @staticmethod
    def purchase_date(po, row):
        return row.get('expected_delivery_date') or row.get('schedule_date') or po.get('schedule_date')

    @staticmethod
    def purchase_balance(row):
        return max(0, row['qty'] - (row.get('received_qty') or 0))

    @classmethod
    def inbound_by_item(cls, purchase_orders):
        """Open stock-unit balances and dated commitments visible to the buyer."""
        inbound = {}
        for po in purchase_orders:
            if cls.purchase_unavailable(po):
                continue
            for row in po['items']:
                expected = cls.purchase_date(po, row)
                if expected:
                    balance = cls.purchase_balance(row) * (row.get('conversion_factor') or 1)
                    inbound.setdefault(row['item_code'], []).append((expected, balance))
        return inbound

    def review_supplier_delays(self, orders, stock, use, active):
        today = self.date()
        confirmed = self.inbound_by_item(po for po in orders.values() if po['docstatus'] == 1)
        for name, po in orders.items():
            lines = {row['name']: row for row in po['items']}
            pending = active.get(name, {})
            for line in sorted(lines.keys() | pending.keys()):
                row = lines.get(line)
                case = pending.get(line)
                if not case and (po['docstatus'] != 1 or self.purchase_unavailable(po)):
                    continue
                date = self.purchase_date(po, row) if row else None
                balance = self.purchase_balance(row) if row else 0
                item = row['item_code'] if row else case['item']
                available = stock.get((item, self.wh['rm']), 0)
                # Stock needed before another order can arrive. Same-day PO dates still
                # count as scheduled supply. Overdue dates do not cover this review.
                vendor = self.catalog.source_list.get(item)
                lead = self.catalog.vendors[vendor]['supplies'][item][1][0] if vendor else 1
                horizon = self.dated(self.day + lead)
                timely = sum(qty for eta, qty in confirmed.get(item, []) if today <= eta <= horizon)
                needed = use.get(item, 0) * lead
                if po.get('workflow_state') == 'Rejected':
                    reason = 'Purchase order rejected. Reassess demand through normal purchasing.'
                elif po['docstatus'] == 1 and row and not balance:
                    reason = 'Native PO balance is fully received. Check released coverage through normal planning.'
                elif po['docstatus'] != 1 or self.purchase_unavailable(po):
                    reason = f"Purchase order is {po.get('status') or 'no longer submitted'}. Reassess coverage."
                elif not row:
                    reason = 'Purchase order line removed. Reassess coverage.'
                elif not date or date >= today:
                    reason = 'Native delivery schedule no longer overdue. Reassess coverage against the observed date.'
                elif available + timely >= needed:
                    reason = 'Released stock and dated inbound cover current lead-time demand.'
                else:
                    reason = None

                if not case and reason:
                    continue
                date_label = 'Expected Delivery Date' if row and row.get('expected_delivery_date') else 'Required By'
                observation = (f"As of {today}, PO {escape(name)}, line {escape(line)}, material {escape(item)}. "
                               f"Native {date_label} {date or 'unavailable'}. Open balance {balance:g} "
                               f"{escape((row or {}).get('uom') or 'order units')}. "
                               f"Released stock {available:g}, timely inbound {timely:g}, "
                               f"lead-time demand {needed:g} (stock units).")
                snapshot = [date, balance]
                if case and not reason and case['snapshot'] == snapshot and case['review'] and case['review'] > today:
                    continue
                review = case['review'] if reason and case['review'] else self.dated(self.day + 1)
                if case and snapshot != case['snapshot']:
                    prior_date, prior_balance = case['snapshot']
                    observation += (f" Previous native delivery date {prior_date or 'unavailable'}, "
                                    f"open balance {prior_balance:g} order units.")
                if reason:
                    description = observation + ' ' + reason
                else:
                    description = (observation + f' Buyer to check supplier status and review coverage by {review}. '
                                   'Route any substitution or expedite spend to SCM for separate approval.')
                fields = dict(reference_type='Purchase Order', reference_name=name,
                              assigned_by='scm_mgr@norvale.test', allocated_to='buyer@norvale.test',
                              date=review, status='Closed' if reason else 'Open', description=description)
                if not case:
                    base = f'supplier-delay:{name}:{line}:{today}'
                    key = base
                    episode = 1
                    while key in self.docs:
                        episode += 1
                        key = f'{base}:{episode}'
                    case = dict(key=key, item=item, snapshot=snapshot, review=None)
                    active.setdefault(name, pending)[line] = case
                    self.persist()
                # Persist the episode before the POST. If supplier evidence changes
                # after a lost reply, recover the same task and reconcile its fields.
                task = self.write(case['key'], 'ToDo', fields, 'SCM_MGR')
                if any(task.get(k) != v for k, v in fields.items()):
                    self.api['SCM_MGR'].update('ToDo', task['name'], fields)
                task = self.api['BUYER'].get('ToDo', task['name'])
                if any(task.get(k) != v for k, v in fields.items()):
                    raise ValueError('Supplier delay task differs from buyer readback')
                self.log('supplier_delay_review', role='BUYER', as_of=today, doc=task)
                if reason:
                    del pending[line]
                    if not pending:
                        del active[name]
                else:
                    case.update(snapshot=snapshot, review=review)
                self.persist()

    def replenishment(self):
        ops=self.state['ops']
        active = ops.setdefault('supplier_delays', {})
        stock = self.inventory('BUYER')
        use = self.observed_usage()
        submitted = self.observe('BUYER', 'Purchase Order', {
            'company': self.company, 'docstatus': 1,
            'status': ['not in', ['Closed', 'Cancelled', 'Completed', 'On Hold']],
        }, ['name'])
        # Active reviews also need terminal orders, which disappear from the open list.
        orders = {name: self.api['BUYER'].get('Purchase Order', name)
                  for name in sorted({row['name'] for row in submitted}
                                     | {name for name, cases in active.items() if cases})}
        self.review_supplier_delays(orders, stock, use, active)

        def build():
            tracked = [self.get(key, 'BUYER') for key, p in ops['purchases'].items() if not p.get('received')]
            tracked_names = {d['name'] for d in tracked}
            inbound = self.inbound_by_item(tracked + [po for name, po in orders.items()
                                                    if name not in tracked_names])
            groups={}
            for code,daily in use.items():
                if daily<=0 or code=='WATER':continue
                if code not in self.catalog.source_list:
                    self.log('blocked', role='BUYER', item=code, reason='No recurring supplier policy')
                    continue
                vendor=self.catalog.source_list[code];cost,(lead,sd),_=self.catalog.vendors[vendor]['supplies'][code]
                master=self.api['BUYER'].get('Item',code)
                configured=next((r['warehouse_reorder_level'] for r in master.get('reorder_levels',[]) if r['warehouse']==self.wh['rm']),0)
                targets=[p['reorder_target'] for p in self.effective_policies('SCM_MGR') if code in p['items']]
                threshold=max([daily*(lead+self.catalog.safety_days[code]),configured]+targets)
                coverage_end = self.dated(self.day + math.ceil(lead + self.catalog.safety_days[code]))
                # Overdue open commitments still need receipt or buyer resolution. Counting
                # them avoids issuing a duplicate order solely because a promised date passed.
                on_order = sum(q for promised, q in inbound.get(code, []) if promised <= coverage_end)
                current=stock.get((code,self.wh['rm']),0)+on_order
                if current<threshold:
                    qty=max(12,math.ceil(max(daily*8,threshold-current)/12)*12)
                    groups.setdefault(vendor,[]).append({'item_code':code,'qty':qty,'rate':round(cost*self.regime()['cost'],4),'schedule_date':self.dated(self.day+lead),'warehouse':self.wh['hold']})
            return [{'key':f'd{self.day:04}-purchase-{vendor}','vendor':vendor,'items':rows,'day':self.day,'due':self.day+max(self.catalog.vendors[vendor]['supplies'][r['item_code']][1][0] for r in rows)} for vendor,rows in groups.items()]
        for p in self.plan(build):
            quotation=self.write('quote:'+p['key'],'Supplier Quotation',{'company':self.company,'supplier':self.get('vendor:'+p['vendor'])['name'],'transaction_date':self.date(),'valid_till':self.dated(self.day+7),'items':p['items']},'BUYER',True)
            quote_rows={r['item_code']:r for r in quotation['items']}
            po_items=[{**r,'supplier_quotation':quotation['name'],'supplier_quotation_item':quote_rows[r['item_code']]['name']} for r in p['items']]
            self.write(p['key'],'Purchase Order',{'company':self.company,'supplier':self.get('vendor:'+p['vendor'])['name'],'schedule_date':self.dated(p['due']),'items':po_items},'BUYER')
            ops['purchases'].setdefault(p['key'],p);self.persist()
            self.mail(p['key'],'BUYER','SCM_MGR','Replenishment approval requested',f"Native purchase {self.get(p['key'])['name']} replenishes released inventory below lead-time plus safety coverage.")

    def schedule_supplier_arrival(self, key, purchase, submitted):
        from native.company_erp import Pending
        if 'arrival_due' in purchase or purchase.get('world_dispatched'):
            return
        if submitted.get('docstatus') != 1:
            raise Pending('Supplier dispatch requires a submitted purchase order')
        dates = [row.get('schedule_date') or submitted.get('schedule_date') for row in submitted['items']]
        if not dates or not all(dates):
            raise Pending('Submitted purchase order lacks a promised delivery date')
        promised = (datetime.fromisoformat(max(dates)).date() -
                    datetime.fromisoformat(self.dated(0)).date()).days
        if self.config.get('world'):
            from native.company_world import dispatch
            dispatch(self, key, submitted, promised)
            purchase.update(world_dispatched=True, approved_day=self.day)
            return
        rng = random.Random(self.config['seed'] + int(hashlib.sha256(key.encode()).hexdigest()[:8], 16))
        delay = math.ceil((self.regime()['supply'] - 1) * max(1, promised - purchase['day']))
        purchase['arrival_due'] = max(self.day + 1, promised + delay + rng.choice([-1, 0, 0, 2]))
        purchase['approved_day'] = self.day

    def purchase_decision(self, key, role, evidence, question, **options):
        from router import ProviderUnavailable
        try:
            return self.choose(key, role, evidence, question, **options)
        except ProviderUnavailable as exc:
            if not self.config.get('company_operating'):
                raise
            result = dict(action='defer', reason=str(exc), message='Purchase remains pending review.')
            self.state['decisions'][key] = dict(mode='provider-unavailable', review_on=self.dated(self.day + 2),
                input=dict(role=role, as_of=self.date(), evidence=evidence), output=result)
            self.persist()
            self.log('purchase_review_unavailable', decision=key, role=role, reason=str(exc))
            return result

    def approvals(self):
        ops=self.state['ops']
        purchase_docs={key:self.get(key,'SCM_MGR') for key,p in ops['purchases'].items() if not p.get('received')}
        for key,p in list(ops['purchases'].items()):
            if p.get('received'):continue
            po=purchase_docs[key]
            if self.purchase_unavailable(po):
                continue
            if po['docstatus']==1:
                if 'arrival_due' not in p and not p.get('world_dispatched'):
                    self.schedule_supplier_arrival(key, p, po)
                    self.persist()
                continue
            if self.day<=p['day'] or self.day<p.get('review_after',0):continue
            first=not any(k.startswith('po-review:') for k in self.state['decisions'])
            usage=self.observed_usage()
            coverage=self.observe('SCM_MGR','Bin',{'item_code':['in',[r['item_code'] for r in po['items']]]},['item_code','warehouse','actual_qty'])
            evidence={k:po.get(k) for k in ['name','supplier','grand_total']}
            evidence['items']=[{k:r.get(k) for k in ['item_code','item_name','qty','uom','rate','amount','schedule_date']} for r in po['items']]
            quote=self.get(p.get('latest_quote','quote:'+key),'SCM_MGR')
            if quote.get('valid_till') and quote['valid_till']<self.date():
                latest_quote='requote:'+key+':'+str(self.day)
                quote=self.write(latest_quote,'Supplier Quotation',{'company':self.company,'supplier':po['supplier'],'transaction_date':self.date(),'valid_till':self.dated(self.day+7),'items':p['items']},'BUYER',True)
                p['latest_quote']=latest_quote;self.persist()
            evidence['supplier_receipts']=self.observe('SCM_MGR','Purchase Receipt',{'supplier':po['supplier'],'docstatus':1},['name','posting_date','supplier'])
            evidence['supplier_quote']={k:quote.get(k) for k in ['name','supplier','transaction_date','valid_till','grand_total']}
            evidence['supplier_quote']['items']=[{k:r.get(k) for k in ['item_code','qty','rate','amount']} for r in quote['items']]
            evidence['receiving_policy']='Purchased materials are received into Quarantine, inspected by QA, then transferred to Raw Materials. Zero optional historical price fields indicate no previous purchase, not a conflicting quote.'
            coverage=[{'item_code':row['item_code'],'released_raw_materials_qty':sum(b['actual_qty'] for b in coverage if b['item_code']==row['item_code'] and b['warehouse']==self.wh['rm']),'quarantine_qty':sum(b['actual_qty'] for b in coverage if b['item_code']==row['item_code'] and b['warehouse']==self.wh['hold']),'open_po_qty_excluding_this':sum(max(0,r['qty']-r.get('received_qty',0)) for other in purchase_docs.values() if other['name']!=po['name'] and other['docstatus'] in (0,1) and other.get('workflow_state')!='Rejected' for r in other['items'] if r['item_code']==row['item_code'])} for row in po['items']]
            evidence.update(coverage=coverage,observed_daily_usage={r['item_code']:usage.get(r['item_code'],0) for r in po['items']},other_open_orders=self.observe('SCM_MGR','Purchase Order',{'docstatus':1,'status':['not in',['Closed','Completed']]},['name','supplier','grand_total']))
            if self.config.get('world'):
                from native.company_world import evidence as world_evidence
                evidence['carrier_observations'] = world_evidence(self, 'SCM_MGR')
            decision_key = 'po-review:'+key+':'+str(self.day)
            result=self.purchase_decision(decision_key,'SCM_MGR',evidence,'Decide this PO on its own current evidence: approve, defer, reject or investigate. Separately and optionally propose a scoped standing_policy effective tomorrow and reviewed in 14 days. Not having grounds for a policy is not a reason to defer this PO. The approval_limit applies per future PO; other_open_orders show observed supplier totals. Assess current observations rather than copying earlier reasons. Quarantine is the established receiving/inspection path, not a location conflict.',model=first)
            if result['action'] in ('approve','reject'):
                action='Approve' if result['action']=='approve' else 'Reject'
                self.api['SCM_MGR'].call('frappe.model.workflow.apply_workflow', {'doc':json.dumps(po), 'action':action})
                updated = self.get(key, 'SCM_MGR')
                self.log('workflow_readback', role='SCM_MGR', doc=updated)
                if action == 'Approve':
                    self.schedule_supplier_arrival(key, p, updated)
            else:
                review_after = self.day + 2
                fields = dict(description=result['reason']+'; review '+self.dated(review_after),
                              reference_type='Purchase Order', reference_name=po['name'],
                              allocated_to='buyer@norvale.test')
                decision = self.state['decisions'].get(decision_key, {})
                if decision.get('mode') == 'provider-unavailable' and decision.get('review_on'):
                    fields['date'] = decision['review_on']
                task = self.write('pending:'+key+':'+str(self.day), 'ToDo', fields, 'SCM_MGR')
                if 'date' in fields:
                    confirmed = self.api['BUYER'].get('ToDo', task['name'])
                    if any(confirmed.get(field) != value for field, value in fields.items()):
                        raise RuntimeError('Purchase buyer follow-up differs from the pending review')
                p['review_after'] = review_after
            self.persist()
        for key,c in list(ops['changes'].items()):
            if c.get('implemented'):continue
            if 'approved_day' not in c:
                if self.day<=c['requested_day'] or self.day<c.get('review_after',0):continue
                request=self.get(key,'SCM_MGR')
                result=self.choose('change-review:'+key+':'+str(self.day),'SCM_MGR',{'request':request,'stock':self.observe('SCM_MGR','Bin',{'item_code':c['item'],'warehouse':self.wh['rm']},['item_code','actual_qty'])},'Approve this evidence-grounded reorder-level change, or defer/reject/investigate.',model=True)
                if result['action']=='approve':
                    self.write('approval:'+key,'ToDo',{'reference_type':'ToDo','reference_name':request['name'],'description':json.dumps({'approved_item':c['item'],'reorder_level':c['level']}),'allocated_to':'it_admin@norvale.test'},'SCM_MGR')
                    c['approved_day']=self.day
                else:c['review_after']=self.day+7
            elif self.day>c['approved_day']:
                approval=self.get('approval:'+key,'IT_ADMIN');request=self.get(key,'IT_ADMIN')
                assert approval['owner']=='scm_mgr@norvale.test' and approval['reference_name']==request['name']
                payload=json.loads(approval['description']);assert payload=={'approved_item':c['item'],'reorder_level':c['level']}
                doc=self.api['IT_ADMIN'].update('Item',c['item'],{'reorder_levels':[{'warehouse':self.wh['rm'],'warehouse_reorder_level':c['level'],'warehouse_reorder_qty':c['level'],'material_request_type':'Purchase'}]})
                assert self.api['IT_ADMIN'].get('Item',c['item'])['reorder_levels'][0]['warehouse_reorder_level']==c['level']
                for d in [approval,request]:self.api['IT_ADMIN'].update('ToDo',d['name'],{'status':'Closed'})
                c['implemented']=self.day;self.log('master_implemented',doc=doc)
            self.persist()

    def record_supplier_arrival(self, key, purchase):
        if 'arrived_day' not in purchase:
            purchase['arrived_day'] = self.day
            self.state['physical'][key] = {'arrived_day': self.day, 'items': purchase['items'], 'booked': False}
            self.persist()
            self.log('physical_arrival', purchase=key, items=purchase['items'])

    def arrivals(self):
        for key,p in self.state['ops']['purchases'].items():
            if 'received' in p or 'arrival_due' not in p or self.day<p['arrival_due']:continue
            po = self.get(key, 'WAREHOUSE')
            if (self.purchase_unavailable(po) or po['docstatus'] != 1
                    or not any(self.purchase_balance(row) for row in po['items'])):
                # A receipt may have committed before the previous controller stopped.
                # Recover that operation, while leaving other native receipts untouched.
                receipt = self.recover_company_doc('receipt:'+key, 'Purchase Receipt', 'WAREHOUSE')
                if not receipt or receipt['docstatus'] != 1:
                    continue
            if 'arrived_day' not in p:
                self.record_supplier_arrival(key, p)
            if self.day<p['arrived_day']+self.config['receipt_delay']:continue
            lot_flow = getattr(self, 'company_erp', None)
            def receive():
                doc = self.mapped('WAREHOUSE', 'erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt',
                                  existing=('receipt:'+key, 'Purchase Receipt'), source_name=self.get(key)['name'])
                for row in doc['items']:
                    row['warehouse'] = self.wh['hold']
                if lot_flow:
                    receipt = lot_flow.prepare_receipt('receipt:'+key, 'Purchase Receipt', doc, 'WAREHOUSE')
                    lot_flow.receive(receipt, 'incoming', 'WAREHOUSE')
                else:
                    self.write('receipt:'+key, 'Purchase Receipt', doc, 'WAREHOUSE', True)
                return True
            if lot_flow:
                if not lot_flow.attempt('receipt:'+key, 'WAREHOUSE', receive):
                    continue
            else:
                receive()
            p['received']=self.day;self.state['physical'][key]['booked']=True;self.persist()

    def inspect_release(self,key,item,qty,reference,raw=False):
        ref=self.get(reference,'QA');m=self.catalog.materials.get(item,{})
        attr=m.get('attr') if raw else None
        low,high=attr[2] if attr else (0,1)
        measured=attr[1] if attr else 0.5
        # Synthetic measured values, not inferred from approval or software status.
        q=self.write('qc:'+key,'Quality Inspection',{'inspection_type':'Incoming' if raw else 'In Process','reference_type':ref['doctype'],'reference_name':ref['name'],'item_code':item,'sample_size':1,'inspected_by':'qa@norvale.test','manual_inspection':1,'status':'Accepted','readings':[{'specification':attr[0] if attr else 'Visual release score (0-1)','reading_1':str(measured),'numeric':1,'min_value':low,'max_value':high}]},'QA',True)
        assert q['status']=='Accepted'
        self.write('release:'+key,'Stock Entry',{'company':self.company,'stock_entry_type':'Material Transfer','items':[{'item_code':item,'qty':qty,'s_warehouse':self.wh['hold'],'t_warehouse':self.wh['rm'] if raw else self.wh['fg']}]},'QA',True)

    def inspections(self):
        if self.config.get('company_operating'):
            from integrations.company_run.operating import Operating
            from integrations.company_run.timing import queue_samples
            queue_samples(self, self.additions, self.additions.now + 1, Operating(self).eligible_samples())
        ops=self.state['ops']
        lot_flow = getattr(self, 'company_erp', None)
        if lot_flow:
            lot_flow.attempt('quality-reconciliation', 'QA', lot_flow.inspections)
        for key,p in ops['purchases'].items():
            if 'received' not in p or 'invoice:'+key in ops['invoices']:continue
            if lot_flow:
                lot_flow.attempt('supplier-invoice:' + key, 'FINANCE', lambda: lot_flow.invoice_purchase(key))
                continue
            if not p.get('released'):
                receipt = self.get('receipt:'+key, 'QA')
                accepted = {}
                for row in receipt['items']:
                    code = row['item_code']
                    accepted[code] = accepted.get(code, 0) + row['qty'] * (row.get('conversion_factor') or 1)
                for code, quantity in accepted.items():
                    if quantity > 0:
                        self.inspect_release(key+':'+code,code,quantity,'receipt:'+key,True)
                p['released']=self.day;self.persist()
            doc=self.mapped('FINANCE','erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice',existing=('invoice:'+key,'Purchase Invoice'),source_name=self.get('receipt:'+key)['name'])
            doc['due_date']=self.dated(self.day+5)
            self.write('invoice:'+key,'Purchase Invoice',doc,'FINANCE',True)
            ops['invoices']['invoice:'+key]={'due':self.day+5};self.persist()
        if lot_flow:
            return
        for key,p in ops['production'].items():
            if p.get('released') or self.day<=p['day']:continue
            self.inspect_release(key,p['item'],p['qty'],'manufacture:'+key)
            p['released']=self.day
            self.state['physical'][key].update(awaits_inspection=False,quality_released_day=self.day)
            self.persist()

    def production(self):
        if self.config.get('company_operating'):
            from integrations.company_run.operating import Operating
            participant = Operating(self)
            participant.definitions()
        self.plan_production()
        if self.config.get('company_operating'):
            from integrations.company_run.timing import queue_dispatches
            queue_dispatches(self, participant)

    def plan_production(self):
        ops=self.state['ops']
        lot_flow = getattr(self, 'company_erp', None)
        if lot_flow:
            lot_flow.retire_overplanned_work()
            lot_flow.poll_work()
        def build():
            if not lot_flow and any(not m.get('closed') for m in ops['maintenance'].values()):return []
            stock=self.inventory('PLANNER');capacity=self.config['capacity_units'];out=[];short=[]
            for key, order in ops['orders'].items():
                row = self.get(key, 'PLANNER')['items'][0]
                outstanding = float(row['qty']) - float(row.get('delivered_qty', 0))
                committed = sum(p['qty'] for p in ops['production'].values() if p['order'] == key)
                remaining = float(row['qty']) - committed
                if outstanding <= 0 or remaining <= 0 or capacity < 12:
                    continue
                bom = self.get('bom:' + order['item'], 'PLANNER')
                component_qty = {r['item_code']: r['qty'] for r in bom['items']}
                available=min(stock.get((code,self.wh['rm']),0)/need for code,need in component_qty.items())
                qty=int(min(remaining,capacity,available)//12)*12
                if qty<=0:
                    missing=[code for code,need in component_qty.items()
                             if stock.get((code,self.wh['rm']),0)<need*min(remaining,12)]
                    short.extend(missing);continue
                p={'key':f'd{self.day:04}-work-'+key,'order':key,'item':order['item'],'qty':qty,'day':self.day}
                out.append(p);capacity-=qty
                for code, need in component_qty.items():
                    stock[(code,self.wh['rm'])]=stock.get((code,self.wh['rm']),0)-need*qty
            return {'work':out,'shortages':sorted(set(short))}
        plan=self.plan(lot_flow.build_plan if lot_flow else build)
        if plan==[]:self.log('blocked',reason='Maintenance owns unavailable equipment');return
        for item in plan['shortages']:
            ops['shortage_days'].setdefault(item,[])
            if self.day not in ops['shortage_days'][item]:ops['shortage_days'][item].append(self.day)
            key='shortage:'+item
            task=self.write(key,'ToDo',{'description':f'Planner observed missing released {item} for pending sales orders. Replenishment review required.','allocated_to':'scm_mgr@norvale.test','date':self.dated(self.day+1)},'PLANNER')
            if len(ops['shortage_days'][item])>=2 and not any(c['item']==item for c in ops['changes'].values()):
                change='change:'+item;level=math.ceil(self.observed_usage().get(item,1)*12)
                self.write(change,'ToDo',{'description':f'Repeated native inventory shortage for {item}, observed days {ops["shortage_days"][item]}. Request reorder level {level}. Approval is not implementation.','allocated_to':'scm_mgr@norvale.test'},'BUYER')
                ops['changes'][change]={'item':item,'level':level,'requested_day':self.day}
                self.mail(change,'BUYER','SCM_MGR','Repeated shortage: master data request',self.get(change)['description'])
            self.persist()
        if lot_flow:
            lot_flow.production(plan)
            return
        for p in plan['work']:
            from systems_v2 import operation_bom,job_cards,complete_job
            minutes_per_unit=self.config.get('shift_minutes',480)/self.config['capacity_units']
            routed_bom=operation_bom(self,p['item'],minutes_per_unit)
            wo=self.write(p['key'],'Work Order',{'production_item':p['item'],'bom_no':routed_bom['name'],'operations':[{'operation':r['operation'],'workstation':r['workstation'],'time_in_mins':p['qty']*minutes_per_unit,'batch_size':p['qty'],'bom':routed_bom['name'],'status':'Pending'} for r in routed_bom['operations']],'transfer_material_against':'Work Order','qty':p['qty'],'company':self.company,'use_multi_level_bom':0,'skip_transfer':1,'source_warehouse':self.wh['rm'],'wip_warehouse':self.wh['wip'],'fg_warehouse':self.wh['hold'],'planned_start_date':self.date()+' 08:00:00','sales_order':self.get(p['order'])['name']},'PLANNER',True)
            offset=sum(w['qty'] for w in plan['work'][:plan['work'].index(p)])
            start=datetime.fromisoformat(self.date()+' 08:00:00')+timedelta(minutes=offset*minutes_per_unit)
            end=start+timedelta(minutes=p['qty']*minutes_per_unit)
            for card in job_cards(self,wo['name']):
                actual=complete_job(self,card['name'],self.state['identities']['PRODUCTION']['employee'],start.isoformat(' '),end.isoformat(' '),p['qty'])
                self.log('readback',role='PRODUCTION',doc=actual)
                self.docs['job-card:'+p['key']]={'doctype':'Job Card','name':actual['name']};self.persist()
            doc=self.mapped('PRODUCTION','erpnext.manufacturing.doctype.work_order.work_order.make_stock_entry',existing=('manufacture:'+p['key'],'Stock Entry'),work_order_id=wo['name'],purpose='Manufacture',qty=p['qty'])
            for r in doc['items']:
                if r.get('is_finished_item'):r['t_warehouse']=self.wh['hold']
                else:r['s_warehouse']=self.wh['rm']
            self.write('manufacture:'+p['key'],'Stock Entry',doc,'PRODUCTION',True)
            ops['production'].setdefault(p['key'],p)
            ops['meter']=sum(w['qty'] for w in ops['production'].values())
            self.state['physical'][p['key']]={'produced':p['qty'],'completed_day':self.day,'awaits_inspection':True};self.persist()
            self.log('physical_production',native_work_order=wo['name'],item=p['item'],qty=p['qty'],completed_day=self.day)

    def fulfillment(self):
        if getattr(self, 'company_erp', None):
            self.company_erp.fulfillment()
            return
        ops=self.state['ops']
        def build():
            stock=self.inventory('WAREHOUSE');out=[]
            open_names={r['name'] for r in self.observe('WAREHOUSE','Sales Order',{'docstatus':1,'per_delivered':['<',100]},['name'])}
            for key,p in ops['orders'].items():
                if self.docs[key]['name'] not in open_names:continue
                doc=self.get(key,'WAREHOUSE');row=doc['items'][0]
                qty=min(row['qty']-row.get('delivered_qty',0),stock.get((p['item'],self.wh['fg']),0))
                if qty<=0:continue
                out.append({'key':f'd{self.day:04}-delivery-'+key,'order':key,'qty':qty,'item':p['item']});stock[(p['item'],self.wh['fg'])]-=qty
            return out
        for p in self.plan(build):
            doc=self.mapped('WAREHOUSE','erpnext.selling.doctype.sales_order.sales_order.make_delivery_note',existing=(p['key'],'Delivery Note'),source_name=self.get(p['order'])['name'])
            doc['items'][0]['qty']=p['qty'];self.write(p['key'],'Delivery Note',doc,'WAREHOUSE',True)
            self.state['physical'][p['key']]={'departed_day':self.day,'qty':p['qty'],'item':p['item']};self.persist()
            self.log('physical_dispatch',native_delivery=self.docs[p['key']]['name'],item=p['item'],qty=p['qty'],departed_day=self.day)
            self.invoice_delivery(p['key'], self.day)

    def invoice_delivery(self, key, departed_day):
        invoice_key = 'invoice:' + key
        invoices = self.state['ops']['invoices']
        if invoice_key in invoices:
            return
        delivery = self.get(key, 'WAREHOUSE' if getattr(self, 'company_erp', None) else None)
        doc = self.mapped('FINANCE', 'erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice',
                          existing=(invoice_key, 'Sales Invoice'), source_name=delivery['name'])
        doc['due_date'] = self.dated(max(self.day, departed_day + 3))
        if getattr(self, 'company_erp', None):
            from native.company_erp import Pending

            fields = ('item_code', 'qty', 'uom', 'conversion_factor', 'stock_qty', 'rate')
            expected = {row['name']: tuple(row[field] for field in fields) for row in delivery['items']}

            def verify(invoice):
                billed = {row.get('dn_detail'): tuple(row.get(field) for field in fields)
                          for row in invoice['items'] if row.get('delivery_note') == delivery['name']}
                if (delivery['docstatus'] != 1 or delivery.get('is_return') or invoice.get('is_return')
                        or invoice.get('update_stock') or len(billed) != len(invoice['items']) or billed != expected
                        or any(invoice.get(k) != delivery[k] for k in ('customer', 'company', 'currency'))):
                    raise Pending('Invoice differs from the confirmed delivery')

            verify(doc)
            actual = self.write(invoice_key, 'Sales Invoice', doc, 'FINANCE', True, before_submit=verify)
            verify(actual)
        else:
            self.write(invoice_key, 'Sales Invoice', doc, 'FINANCE', True)
        invoices[invoice_key] = {'due': departed_day + 3}
        self.persist()

    def bank_statements(self):
        from native.company_bank import BankReceipts
        BankReceipts(self.company_erp).import_statements()

    def settlements(self):
        if (self.config or {}).get('company_operating'):
            from native.company_bank import BankReceipts
            bank = BankReceipts(self.company_erp)
            for key, invoice in self.state['ops']['invoices'].items():
                if invoice.get('paid') is not None:
                    continue
                # Look for prepayments too, but only flag absent evidence once due.
                result = self.company_erp.attempt('settle:' + key, 'FINANCE',
                                                  lambda: bank.settle_invoice(key, overdue=self.day >= invoice['due']))
                if result is None:
                    continue
                payments, outstanding = result
                if payments:
                    invoice['payment'] = payments[-1]
                    invoice['payments'] = payments
                    if outstanding == 0:
                        invoice['paid'] = self.day
                    self.persist()
            return
        for key,p in self.state['ops']['invoices'].items():
            if p.get('paid') is not None or self.day<p['due']:continue
            if getattr(self, 'company_erp', None):
                payment = self.company_erp.attempt('settle:'+key, 'FINANCE', lambda:self.company_erp.settle(key))
                if not payment:
                    continue
                p['payment'] = payment
            else:
                self.pay('pay:'+key,key)
            p['paid']=self.day;self.persist()

    def service(self):
        if getattr(self, 'company_erp', None):
            return self.company_erp.equipment.service()
        from systems_v2 import Tickets
        tickets=Tickets(self.run)
        ops=self.state['ops']
        title = 'Filler preventive service'
        for key,m in ops['maintenance'].items():
            if m.get('closed') or self.day<m['due']:continue
            doc=self.get(key,'MAINTENANCE')
            completion = ' Technician inspection and restart test completed ' + self.date()
            if doc['status']!='Closed':
                self.api['MAINTENANCE'].update('ToDo',doc['name'],{'status':'Closed','description':doc['description']+completion})
            issue_body = m.get('issue_body')
            if issue_body is None:
                # Older checkpoints only kept the issue number and opening day.
                issue_body = (doc['description'].removesuffix(completion) + ' ERP task ' + doc['name']
                              + '; reported ' + self.dated(m['day']))
            ticket = tickets.close(m['ticket'], completion.strip() + '. ERP task ' + doc['name'],
                                   key=key, title=title, body=issue_body)
            self.log('ticket_readback',record=ticket)
            m['closed']=self.day;ops['serviced_meter']=m['meter'];self.log('maintenance_readback',doc=self.get(key,'MAINTENANCE'));self.persist()
        if ops['meter']-ops['serviced_meter']>=self.config['maintenance_units'] and not any(not m.get('closed') for m in ops['maintenance'].values()):
            key=f'service:{ops["meter"]}'
            self.write(key,'ToDo',{'description':f'Filler preventive service due after {ops["meter"]-ops["serviced_meter"]} produced units. Equipment unavailable until technician restart test.','allocated_to':'maintenance@norvale.test'},'MAINTENANCE')
            task = self.get(key)
            issue_body = task['description'] + ' ERP task ' + task['name'] + '; reported ' + self.date()
            ticket = tickets.open(key, title, issue_body)
            self.log('ticket_readback',record=ticket)
            self.mail(key,'MAINTENANCE','PLANT_MGR','Filler unavailable for service',self.get(key)['description'])
            ops['maintenance'][key]={'day':self.day,'due':self.day+self.config['maintenance_days'],'meter':ops['meter'],'ticket':ticket['number'],'issue_body':issue_body};self.persist()

    def management(self):
        if (self.run/'private/expert-inbox-v1').is_dir():
            from norvale.agents.expert_inbox import answer_pending
            answer_pending(self.run)
        due = (self.day + 1) % self.config['report_every'] == 0
        binding = self.run/'env/planning-report.json'
        if binding.is_file():
            from integrations.company_run.planning_report import reconcile
            pending = self.state.setdefault('pending_planning_reports', [])
            if due and self.date() not in pending and f'planning-backlog:{self.date()}' not in self.docs:
                pending.append(self.date())
                self.persist()
            reconcile(self, binding)
        if not due:
            return
        quarter = (self.day+1)//90
        review_key = f'review:{self.day}'
        key=f'report:{self.day}'
        period=self.config['report_every']/30
        self.write('costs:'+key,'Journal Entry',{'company':self.company,'posting_date':self.date(),'accounts':[{'account':'Salary - NCC','debit_in_account_currency':round(2500*period,2)},{'account':'Utility Expenses - NCC','debit_in_account_currency':round(300*period,2)},{'account':'Cash - NCC','credit_in_account_currency':round(2500*period,2)+round(300*period,2)}],'user_remark':'Prorated aggregate wages/utilities; prototype, not employee payroll'},'FINANCE',True)

        gl=self.observe('CFO','GL Entry',{'company':self.company,'posting_date':['<=',self.date()]},['account','debit','credit','voucher_type','voucher_no'])
        totals={}
        for r in gl:totals[r['account']]=round(totals.get(r['account'],0)+r['debit']-r['credit'],2)
        pending=self.observe('CFO','ToDo',{'status':'Open'},['name','description','allocated_to'])
        self.write(key,'Note',{'title':'Operating review '+self.date(),'public':0,'content':'<pre>'+json.dumps({'as_of':self.date(),'balances':totals,'outstanding_tasks':pending},indent=2)+'</pre>'},'CFO')
        urgent=any('urgent' in r.get('description','').lower() for r in pending)
        if not urgent and quarter == self.state.get('management_quarter', 0) and review_key not in self.state['decisions']:return
        result=self.choose(review_key,'CFO',{'report':self.get(key,'CFO')['name'],'native_balances':totals,'pending_requests':pending},'Review current financial records and pending requests. Approve the review or investigate/defer/reject with a concrete next request. Do not assume unseen production facts.',action='investigate',model=True)
        if result['action']!='approve':self.write('followup:'+key,'ToDo',{'description':result['reason'],'allocated_to':'finance@norvale.test','date':self.dated(self.day+3)},'CFO')
        self.mail(key,'CFO','CEO','Periodic operating review',result['message'])
        # The stage persists the checkpoint and completion together.
        self.state['management_quarter']=quarter
    def recurring_events(self):
        yield 0,'seed',self.seed
        yield 0,'recurring-setup',self.recurring_setup
        completed = set(self.state['completed']) if self.config.get('company_operating') else set()
        for day in range(self.config['days']):
            for phase in ['service','arrivals','inspections','demand','fulfillment','production','replenishment','approvals','settlements','management']:
                if (phase == 'settlements' and self.config.get('company_operating')
                        and f'd{day:04}:settlements' not in completed):
                    yield day, f'd{day:04}:bank-statements', self.bank_statements
                yield day,f'd{day:04}:{phase}',getattr(self,phase)
            if 'company_timing' in self.config:
                yield day, f'd{day:04}:native-clock', lambda day=day: self.run_additions((day + 1) * 1440 - 1)

    def through(self,day):
        """Safe Run All boundary: repeating a cell cannot pass its absolute day."""
        while self.step(through_day=day) is not None:
            pass

    def extend(self,days,demand_scale=None):
        if days<self.config['days']:raise ValueError('Cannot shorten recorded horizon')
        completed = set(self.state['completed'])
        if any(k not in completed for _,k,_ in self.events()):raise ValueError('Finish current horizon before changing its future demand')
        before=dict(self.config);self.config['days']=days
        if demand_scale is not None:self.config['demand_scale']=float(demand_scale)
        self.validate_config(self.config);save(self.run/'config.json',self.config)
        self.log('horizon_extended',previous=before,new=self.config,effective_day=before['days'])

    @staticmethod
    def validate_config(c):
        from engine.world import validate
        validate(c)
        if not 1<=c['days']<=365:raise ValueError('days must be 1..365')
        if not 1<=c['orders_per_day']<=18:raise ValueError('orders_per_day must be 1..18')
        if not 0<c['demand_scale']<=1:raise ValueError('demand_scale must be in (0,1]')
        for k in ['capacity_units','maintenance_units','maintenance_days','report_every','receipt_delay']:
            if c[k]<=0:raise ValueError(k+' must be positive')
        if 'company_authority' in c:
            factory = c['company_authority']
            if not isinstance(factory, str) or len(factory.split(':')) != 2 or not all(factory.split(':')):
                raise ValueError('company_authority must be module:factory')
        if 'company_operating' in c and (c['company_operating'] is not True
                or 'company_timing' not in c
                or c.get('company_authority') != 'integrations.company_run.authority:Authority'):
            raise ValueError('Operating mode requires native company authority and timing')
        if 'supplier_quality' in c and (type(c['supplier_quality']) is not bool or not c.get('company_operating')):
            raise ValueError('Supplier quality sampling requires an ordinary operating run and a boolean setting')
        if 'company_timing' in c:
            from engine import _calendars
            timing = c['company_timing']
            if (not c.get('company_authority') or not isinstance(timing, dict)
                    or type(timing.get('lab_minutes')) is not int or timing['lab_minutes'] <= 0
                    or not {'PRODUCTION', 'LAB'} <= _calendars(timing).keys()
                    or type(c.get('shift_minutes', 480)) is not int or c.get('shift_minutes', 480) <= 0):
                raise ValueError('Company timing requires authority, PRODUCTION and LAB shifts, and positive minutes')
            for field in ('staff', 'task_minutes'):
                values = timing.get(field, {})
                if not isinstance(values, dict) or any(type(v) is not int or v <= 0 for v in values.values()):
                    raise ValueError(f'Company timing {field} requires positive integers')
            if 'quality_due_minutes' in timing and (type(timing['quality_due_minutes']) is not int
                                                    or timing['quality_due_minutes'] <= 0):
                raise ValueError('Quality due time must be positive business-clock minutes')
        if c['opening_stock_days']<0:raise ValueError('opening_stock_days must be nonnegative')

    def events(self):
        yield from (self.recurring_events() if self.config else self.fixture_events())

    def fixture_events(self):
        yield 0,'seed',self.seed
        yield 0,'purchasing-workflow',self.workflow_setup
        yield 0,'purchase-request',self.purchase
        yield 1,'purchase-approval',self.approve_purchase
        yield 2,'maintenance-request',self.maintenance_request
        yield 3,'physical-arrival',self.arrive
        yield 4,'native-permissions-and-retry-keys',self.configure
        yield 4,'receipt-booking',self.receipt
        yield 4,'incoming-quality',lambda:self.quality('incoming','SLES',500,'receipt') if 'receipt' in self.docs else None
        yield 4,'supplier-invoice',self.supplier_invoice
        yield 5,'maintenance-completion',self.maintenance_complete
        for i in range(3):
            yield 6,'sales-order:'+str(i),lambda i=i:self.order(i)
        for i in range(3):
            yield 7,'manufacture:'+str(i),lambda i=i:self.manufacture(i)
        for i in range(3):
            yield 8,'product-quality:'+str(i),lambda i=i:self.quality('product:'+str(i),self.catalog.skus[i]['sku_id'],240,'manufacture:'+str(i))
        yield 9,'master-change-request',self.change_request
        yield 10,'master-change-approval',self.approve_change
        for i in range(3):yield 11,'dispatch:'+str(i),lambda i=i:self.ship(i)
        yield 12,'master-change-implementation',lambda:self.implement_change() if 'change-approval' in self.docs else None
        for i in range(3):yield 12,'sales-invoice:'+str(i),lambda i=i:self.invoice(i)
        yield 14,'capital-funding',lambda:self.finance_costs(True)
        yield 15,'supplier-payment',lambda:self.pay('supplier-payment','supplier-invoice')
        for i in range(3):yield 20,'customer-payment:'+str(i),lambda i=i:self.pay('customer-payment:'+str(i),'invoice:'+str(i))
        yield 28,'monthly-wages-and-utilities',self.finance_costs
        yield 29,'monthly-review',self.report
        yield 30,'cfo-review',self.management_review

    def step(self, expected=None, *, through_day=None):
        with (self.run/'execution.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if (self.run/'state.json').exists():
                self.state=json.loads((self.run/'state.json').read_text());self.docs=self.state['docs'];self.day=self.state['day']
            completed = set(self.state['completed'])
            if expected and expected in completed:
                return {'event': expected, 'already_completed': True}
            stage = next((stage for stage in self.events() if stage[1] not in completed), None)
            pending = stage[1] if stage else None
            if expected and expected != pending:
                raise ValueError(f'Next stage is {pending}, not {expected}')
            if through_day is not None and (stage is None or stage[0] > through_day):
                return None
            return self._step(stage)

    def _step(self, stage):
        if stage is not None:
            day, key, action = stage
            self.day=day;self.event=key
            start=time.monotonic()
            try:
                self.run_phase(day, key, action)
            except Exception as exc:
                self.log('failed',error=str(exc));raise
            self.state['completed'].append(key);self.persist()
            result={'event':key,'day':day,'seconds':round(time.monotonic()-start,3),'native_documents':len(self.docs)}
            self.log('completed',**{'result':result});return result
        if self.config:
            self.run_additions(self.config['days']*1440-1)
            if self.config.get('company_operating'):
                from integrations.company_run.lake import scheduled_scan
                scheduled_scan(self, self.config['days'] * 1440 - 1)
            if self.state.get('pending_planning_reports'):
                from integrations.company_run.planning_report import reconcile
                reconcile(self, self.run/'env/planning-report.json')
        return None

    def run_phase(self, day, key, action):
        from native.recurring_extensions import COMPANY_OPEN, DAY, MIDNIGHT
        minute = day * DAY + (COMPANY_OPEN if 'company_timing' in (self.config or {}) else 720)
        if self.config and key.startswith(f'd{day:04}:'):
            self.run_additions(minute)
            # Queued native work can checkpoint a different day and event ID.
            self.day, self.event = day, key
        self.advance_clock(MIDNIGHT + timedelta(minutes=minute))
        if self.config and self.config.get('company_operating') and key.endswith(':management'):
            from integrations.company_run.lake import scheduled_scan
            scheduled_scan(self, minute)
        action()
        if self.config and self.config.get('company_operating'):
            self.day, self.event = day, key
            if key.endswith(':settlements') and self.state.get('company_erp', {}).get('shipments'):
                event_id = f'finance-sales-readback:{day}'
                if not self.additions.has_event(event_id):
                    self.additions.schedule(event_id, 'company-sales-readback', self.additions.now,
                                            payload={}, role='FINANCE')
                self.run_additions(self.additions.now)
            if key.endswith(':native-clock'):
                from integrations.company_run.lake import scheduled_scan
                scheduled_scan(self, (day + 1) * DAY - 1)

    def scheduled_company_event(self, clock, payload):
        if not self.config.get('company_operating'):
            raise RuntimeError('Run scheduled qualification work through integrations.company_run')
        from integrations.company_run.timing import dispatch_event
        return dispatch_event(self, clock, payload)

    def advance_clock(self, target):
        if self.stack.clock.now()<target:
            self.stack.settle();self.stack.clock.advance_to(target)

    def run_additions(self, until):
        from native.recurring_extensions import open_catalog
        if self.additions is None:
            self.additions=open_catalog(self.run, self.config, self.state, self.advance_clock, company=self)
        self.additions.run(until=until, advance=True)

    def run_all(self):
        import urllib.error
        attempts=0
        while True:
            try:event=self.step()
            except (urllib.error.URLError, TimeoutError, ConnectionError, SMTPServerDisconnected,
                    SMTPResponseException, SMTPRecipientsRefused, IMAP4.abort, json.JSONDecodeError, ErpError) as exc:
                transient = True
                if isinstance(exc, ErpError):
                    transient = exc.status in (408, 429, 500, 502, 503, 504)
                elif isinstance(exc, SMTPResponseException):
                    transient = 400 <= exc.smtp_code < 500
                elif isinstance(exc, SMTPRecipientsRefused):
                    transient = all(400 <= code < 500 for code, _ in exc.recipients.values())
                if not transient or attempts>=3:raise
                attempts+=1;self.log('stage_retry',attempt=attempts,error=str(exc));time.sleep(2**attempts);continue
            if event is None:return
            attempts=0;print(json.dumps(event),flush=True)

DEFAULT_CONFIG={'days':120,'seed':42,'demand_scale':0.02,'orders_per_day':3,'opening_stock_days':14,'capacity_units':600,'maintenance_units':3000,'shift_minutes':480,'maintenance_days':2,'report_every':7,'receipt_delay':1}

def writable_run(path):
    path=Path(path).resolve()
    for frozen in (ROOT/'data', RUN):
        frozen=frozen.resolve()
        if path.is_relative_to(frozen) or frozen.is_relative_to(path):
            raise ValueError('Choose a dedicated run directory outside frozen evidence')
    return path

def initialize(path, config=None, *, company_authority=None):
    path=writable_run(path)
    if (path/'config.json').exists():
        existing=json.loads((path/'config.json').read_text())
        if config is not None and config!=existing:raise ValueError('Existing run config differs; choose a new directory or explicitly extend the finished horizon')
    elif (path/'state.json').exists() or (path/'env').exists():raise ValueError('Refusing to repurpose an existing non-recurring run')
    else:
        if path.exists() and any(path.iterdir()):raise ValueError('New run directory must be empty')
        config=dict(DEFAULT_CONFIG) if config is None else config
        Engine.validate_config(config);save(path/'config.json',config)
    if not (path/'env/site-created').exists():provision(path)
    return Engine(path, company_authority=company_authority)

def main(argv=None):
    import argparse
    p=argparse.ArgumentParser()
    mode=p.add_mutually_exclusive_group()
    mode.add_argument('--provision',action='store_true')
    mode.add_argument('--extend',type=Path,help='Queue catalog additions for an existing recurring run')
    p.add_argument('--run',type=Path,default=ACTIVE_RUN)
    p.add_argument('--days',type=int)
    p.add_argument('--company-authority')
    p.add_argument('--company-operating', action='store_true',
                   help='Provision a fresh timed run for the native company participants')
    p.add_argument('--world', action='store_true', help='Enable seeded world processes in a fresh recurring run')
    p.add_argument('--demand-scale',type=float)
    a=p.parse_args(argv)
    try:
        a.run=writable_run(a.run)
        if a.world and not a.provision:
            p.error('--world requires --provision with a fresh run directory')
        if a.company_authority is not None and not a.provision:
            p.error('--company-authority requires --provision')
        if a.company_operating and (not a.provision or a.company_authority):
            p.error('--company-operating requires --provision and selects its company authority')
        if a.extend is not None:
            if a.days is not None or a.demand_scale is not None:
                p.error('--extend cannot change the horizon or demand scale')
            from native.recurring_extensions import submit
            return submit(a.run, a.extend)
        if a.provision:
            config=dict(DEFAULT_CONFIG)
            if a.world:
                os.umask(0o077)
                config['world'] = True
            if a.days is not None:config['days']=a.days
            if a.demand_scale is not None:config['demand_scale']=a.demand_scale
            if a.company_authority is not None:config['company_authority']=a.company_authority
            if a.company_operating:
                from integrations.company_run.timing import DEFAULT_TIMING
                os.umask(0o077)
                config.update(company_operating=True, supplier_quality=True, company_timing=DEFAULT_TIMING,
                              company_authority='integrations.company_run.authority:Authority')
            initialize(a.run,config)
            return
        if a.demand_scale is not None and a.days is None:
            p.error('--demand-scale on an existing run requires --days to extend its future')
        config_path = a.run/'config.json'
        if config_path.exists() and json.loads(config_path.read_text()).get('company_operating'):
            os.umask(0o077)
            if not (a.run/'env/company-sources.json').is_file():
                p.error('Configure native company sources before running the operating participants')
        e=Engine(a.run)
        if a.days is not None:e.extend(a.days,a.demand_scale)
    except (ValueError, OSError) as exc:
        p.error(str(exc))
    e.run_all()


if __name__=='__main__':
    main()
