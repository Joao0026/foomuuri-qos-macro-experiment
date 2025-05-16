#!/usr/bin/env python3

import subprocess
import argparse
import logging
import sys
import os
from pathlib import Path
import shlex
import time
import re

# Configuração de Logging
LOG_FILE = "/var/log/foomuuri-qos-macro.log"
logging.basicConfig(
	level=logging.INFO, # Mudar para logging.DEBUG para ver todos os detalhes de parsing
	format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
	handlers=[
		logging.FileHandler(LOG_FILE),
		logging.StreamHandler(sys.stdout)
	]
)
logger = logging.getLogger("QoSMacroParserValidated")

class QoSEngineMacroParserValidated:
	def __init__(self, foomuuri_config_path="/etc/foomuuri/foomuuri.conf"):
		self.foomuuri_config_path = Path(foomuuri_config_path)
		self.config = {'interfaces': [], 'services': []}
		self.managed_ifbs = {}
		self.IFACE_PREFIX = "QOS_IF_"
		self.SERVICE_PREFIX = "QOS_SRV_"
		self.SERVICE_LIST_MACRO = "QOS_SERVICE_LIST"

	def _run_command(self, cmd, check=True, failure_ok=False, log_output=False):
		try:
			cmd_str = ' '.join(shlex.quote(c) for c in cmd)
			logger.debug(f"Executando comando: {cmd_str}")
			result = subprocess.run(cmd, check=check, capture_output=True, text=True, timeout=20)
			if result.stdout and log_output:
				logger.debug(f"Saida: {result.stdout.strip()}")
			elif result.stdout:
				logger.debug(f"Comando teve saida stdout (nao mostrada)")

			if result.stderr:
				log_level_stderr = logging.DEBUG if failure_ok else logging.WARNING
				is_replace_exists_error = ("RTNETLINK answers: File exists" in result.stderr and ("replace" in cmd or "add" in cmd))
				if not is_replace_exists_error:
					logger.log(log_level_stderr, f"Erros/Warnings do comando: {result.stderr.strip()}")
				else:
					logger.debug(f"Warning (ignorado) ao operar em objeto existente: {result.stderr.strip()}")
			return True
		except subprocess.CalledProcessError as e:
			log_level = logging.DEBUG if failure_ok else logging.ERROR
			logger.log(log_level, f"Falha no comando {cmd_str}: {e.stderr.strip()}")
			if check and not failure_ok:
				raise
			return False
		except subprocess.TimeoutExpired:
			logger.error(f"Comando {cmd_str} excedeu o tempo limite.")
			if check:
				raise
			return False
		except Exception as e:
			logger.error(f"Erro inesperado ao executar {cmd_str}: {e}")
			if check:
				raise
			return False

	def _validate_rate_ceil(self, value, context_msg):
		if value is None:
			return None
		if not isinstance(value, str):
			logger.warning(f"Valor de banda fornecido para {context_msg} nao e string: '{value}'. Ignorando.")
			return None
		if not re.match(r'^\d+(\.\d+)?\s*(kbit|mbit|gbit|bit)$', value.lower()):
			logger.warning(f"Formato/unidade de banda invalida para {context_msg}: '{value}'. Ignorando.")
			return None
		return value

	def _validate_priority(self, value, context_msg, default_prio=7):
		if value is None:
			return default_prio
		try:
			prio = int(value)
			if not 0 <= prio <= 15:
				logger.warning(f"Prioridade '{value}' para {context_msg} fora do range (0-15). Usando default {default_prio}.")
				return default_prio
			return prio
		except (ValueError, TypeError):
			logger.warning(f"Valor de prioridade invalido '{value}' para {context_msg}. Usando default {default_prio}.")
			return default_prio

	def _validate_mark(self, value, context_msg):
		if value is None:
			return None
		if not isinstance(value, str) or not value.lower().startswith("0x"):
			logger.error(f"Valor de marca invalido para {context_msg}: '{value}' (deve ser string hex ex: '0x10').")
			return None
		try:
			return int(value, 16)
		except ValueError:
			logger.error(f"Valor de marca hexadecimal invalido para {context_msg}: '{value}'.")
			return None
			
	def _validate_suffix(self, value, context_msg):
		if value is None:
			return None
		if not isinstance(value, str) or not value.isdigit():
			logger.error(f"Valor de class_id_suffix invalido para {context_msg}: '{value}' (deve ser string numerica).")
			return None
		return value

	def _get_macro_value(self, raw_macros, macro_name, context_msg, is_critical=False, default_value=None):
		value = raw_macros.get(macro_name)
		if value is None:
			log_func = logger.error if is_critical else logger.debug
			log_func(f"Macro {'obrigatorio' if is_critical else 'opcional'} {'em falta' if is_critical else 'nao encontrado'} para {context_msg}: {macro_name}" + (f". Usando default: {default_value}" if not is_critical and default_value is not None else ""))
			return default_value
		logger.debug(f"Macro lido para {context_msg}: {macro_name} = '{value}'")
		return value

	def _parse_macros_from_foomuuri_conf(self):
		logger.info(f"Lendo macros de QoS de: {self.foomuuri_config_path}")
		if not self.foomuuri_config_path.is_file():
			logger.error(f"Ficheiro de configuração Foomuuri não encontrado: {self.foomuuri_config_path}")
			return False

		raw_macros = {}
		in_macro_section = False
		macro_regex = re.compile(r'^\s*([A-Za-z0-9_]+)\s+(?:"([^"]*)"|\'([^\']*)\'|([^\s#]+))')

		try:
			with open(self.foomuuri_config_path, 'r') as f:
				for line_num, line_content in enumerate(f, 1):
					line = line_content.strip()
					if line.lower().startswith("macro {"):
						in_macro_section = True
						continue
					if line == "}":
						in_macro_section = False
						continue
					
					if in_macro_section and not line.startswith("#") and line:
						match = macro_regex.match(line)
						if match:
							macro_name = match.group(1)
							macro_value = match.group(2) or match.group(3) or match.group(4)
							raw_macros[macro_name] = macro_value
		except Exception as e:
			logger.error(f"Erro ao ler ou parsear {self.foomuuri_config_path}: {e}")
			return False

		interface_names_map = {}
		for name, value in raw_macros.items():
			if name.startswith(self.IFACE_PREFIX) and name.endswith("_NAME"):
				key_part = name[len(self.IFACE_PREFIX):-len("_NAME")]
				interface_names_map[key_part] = value
		logger.debug(f"Nomes de interface (chaves macro) encontrados: {list(interface_names_map.keys())}")

		if not interface_names_map:
			logger.error("Nenhum macro de definicao de interface (ex: QOS_IF_ENP1S0_NAME) encontrado. Abortando.")
			return False

		for if_key, if_name_val in interface_names_map.items():
			ctx = f"interface '{if_name_val}' (chave macro {if_key})"
			logger.debug(f"Processando {ctx}")
			if_cfg = {'name': if_name_val}

			if_cfg['ifb'] = self._get_macro_value(raw_macros, f"{self.IFACE_PREFIX}{if_key}_IFB", ctx, is_critical=True)
			if_cfg['total_upload_bw'] = self._validate_rate_ceil(self._get_macro_value(raw_macros, f"{self.IFACE_PREFIX}{if_key}_TOTAL_UPLOAD_BW", ctx, is_critical=True), f"{ctx} total_upload_bw")
			if_cfg['total_download_bw'] = self._validate_rate_ceil(self._get_macro_value(raw_macros, f"{self.IFACE_PREFIX}{if_key}_TOTAL_DOWNLOAD_BW", ctx, is_critical=True), f"{ctx} total_download_bw")
			
			def_up_id = self._get_macro_value(raw_macros, f"{self.IFACE_PREFIX}{if_key}_DEFAULT_UPLOAD_ID", ctx, is_critical=True)
			def_up_prio_str = self._get_macro_value(raw_macros, f"{self.IFACE_PREFIX}{if_key}_DEFAULT_UPLOAD_PRIO", ctx, default_value="7")
			def_up_rate = self._validate_rate_ceil(self._get_macro_value(raw_macros, f"{self.IFACE_PREFIX}{if_key}_DEFAULT_UPLOAD_RATE", ctx, is_critical=True), f"{ctx} default_upload_rate")
			def_up_ceil = self._validate_rate_ceil(self._get_macro_value(raw_macros, f"{self.IFACE_PREFIX}{if_key}_DEFAULT_UPLOAD_CEIL", ctx, is_critical=True), f"{ctx} default_upload_ceil")

			def_dl_id = self._get_macro_value(raw_macros, f"{self.IFACE_PREFIX}{if_key}_DEFAULT_DOWNLOAD_ID", ctx, is_critical=True)
			def_dl_prio_str = self._get_macro_value(raw_macros, f"{self.IFACE_PREFIX}{if_key}_DEFAULT_DOWNLOAD_PRIO", ctx, default_value="7")
			def_dl_rate = self._validate_rate_ceil(self._get_macro_value(raw_macros, f"{self.IFACE_PREFIX}{if_key}_DEFAULT_DOWNLOAD_RATE", ctx, is_critical=True), f"{ctx} default_download_rate")
			def_dl_ceil = self._validate_rate_ceil(self._get_macro_value(raw_macros, f"{self.IFACE_PREFIX}{if_key}_DEFAULT_DOWNLOAD_CEIL", ctx, is_critical=True), f"{ctx} default_download_ceil")

			if not all([if_cfg['ifb'], if_cfg['total_upload_bw'], if_cfg['total_download_bw'], 
						def_up_id, def_up_rate, def_up_ceil,
						def_dl_id, def_dl_rate, def_dl_ceil]):
				logger.error(f"Configuracao de interface base incompleta ou invalida para {ctx}. Ignorando esta interface.")
				continue
			
			if_cfg['default_upload_class'] = {
				'id': def_up_id,
				'priority': self._validate_priority(def_up_prio_str, f"{ctx} default_upload_priority"),
				'rate': def_up_rate, 'ceil': def_up_ceil
			}
			if_cfg['default_download_class'] = {
				'id': def_dl_id,
				'priority': self._validate_priority(def_dl_prio_str, f"{ctx} default_download_priority"),
				'rate': def_dl_rate, 'ceil': def_dl_ceil
			}
			self.config['interfaces'].append(if_cfg)

		if not self.config['interfaces']:
			logger.error("Nenhuma interface WAN foi configurada corretamente a partir dos macros. Abortando.")
			return False

		service_list_str = self._get_macro_value(raw_macros, self.SERVICE_LIST_MACRO, "lista de servicos", is_critical=True)
		if not service_list_str:
			logger.warning(f"Macro {self.SERVICE_LIST_MACRO} nao encontrado ou vazio. Nenhum servico especifico sera configurado.")
			return True

		service_list_str = service_list_str.split('#', 1)[0].strip()
		service_keys = service_list_str.split()
		
		if not service_keys: logger.info("Nenhuma chave de servico encontrada em QOS_SERVICE_LIST apos limpeza."); return True
		logger.debug(f"Chaves de servico para processar: {service_keys}")

		for srv_key_from_list in service_keys:
			srv_ctx = f"servico '{srv_key_from_list}'"
			srv_prefix = f"{self.SERVICE_PREFIX}{srv_key_from_list}_"
			
			mark_val = self._validate_mark(self._get_macro_value(raw_macros, f"{srv_prefix}MARK", srv_ctx, is_critical=True), f"{srv_ctx} mark")
			prio_val_str = self._get_macro_value(raw_macros, f"{srv_prefix}PRIORITY", srv_ctx, default_value="5")
			if mark_val is None: logger.warning(f"Marca invalida ou em falta para {srv_ctx}. Ignorando servico."); continue
			
			srv_cfg = {'mark': mark_val, 'priority': self._validate_priority(prio_val_str, f"{srv_ctx} priority")}
			
			up_sfx = self._validate_suffix(self._get_macro_value(raw_macros, f"{srv_prefix}UPLOAD_SUFFIX", f"{srv_ctx} upload_suffix"), f"{srv_ctx} upload_suffix")
			up_rate = self._validate_rate_ceil(self._get_macro_value(raw_macros, f"{srv_prefix}UPLOAD_RATE_DEFAULT", f"{srv_ctx} upload_rate_default"), f"{srv_ctx} upload_rate_default")
			up_ceil = self._validate_rate_ceil(self._get_macro_value(raw_macros, f"{srv_prefix}UPLOAD_CEIL_DEFAULT", f"{srv_ctx} upload_ceil_default"), f"{srv_ctx} upload_ceil_default")
			up_fprio_str = self._get_macro_value(raw_macros, f"{srv_prefix}UPLOAD_FILTER_PRIO_DEFAULT", f"{srv_ctx} upload_filter_prio", default_value="10")
			if all([up_sfx, up_rate, up_ceil]):
				srv_cfg['upload'] = {'class_id_suffix': up_sfx, 'rate': up_rate, 'ceil': up_ceil, 'filter_priority': self._validate_priority(up_fprio_str, f"{srv_ctx} upload_filter_priority", 10)}

			dl_sfx = self._validate_suffix(self._get_macro_value(raw_macros, f"{srv_prefix}DOWNLOAD_SUFFIX", f"{srv_ctx} download_suffix"), f"{srv_ctx} download_suffix")
			dl_rate = self._validate_rate_ceil(self._get_macro_value(raw_macros, f"{srv_prefix}DOWNLOAD_RATE_DEFAULT", f"{srv_ctx} download_rate_default"), f"{srv_ctx} download_rate_default")
			dl_ceil = self._validate_rate_ceil(self._get_macro_value(raw_macros, f"{srv_prefix}DOWNLOAD_CEIL_DEFAULT", f"{srv_ctx} download_ceil_default"), f"{srv_ctx} download_ceil_default")
			dl_fprio_str = self._get_macro_value(raw_macros, f"{srv_prefix}DOWNLOAD_FILTER_PRIO_DEFAULT", f"{srv_ctx} download_filter_prio", default_value="15")
			if all([dl_sfx, dl_rate, dl_ceil]):
				srv_cfg['download'] = {'class_id_suffix': dl_sfx, 'rate': dl_rate, 'ceil': dl_ceil, 'filter_priority': self._validate_priority(dl_fprio_str, f"{srv_ctx} download_filter_priority", 15)}
			
			srv_cfg['interfaces'] = {}
			for if_key, if_name_val in interface_names_map.items():
				if 'upload' in srv_cfg:
					up_ovr_rate_val = self._validate_rate_ceil(self._get_macro_value(raw_macros, f"{srv_prefix}OVERRIDE_{if_key}_UPLOAD_RATE", f"{srv_ctx} override {if_name_val} upload_rate"), f"{srv_ctx} override {if_name_val} upload_rate")
					up_ovr_ceil_val = self._validate_rate_ceil(self._get_macro_value(raw_macros, f"{srv_prefix}OVERRIDE_{if_key}_UPLOAD_CEIL", f"{srv_ctx} override {if_name_val} upload_ceil"), f"{srv_ctx} override {if_name_val} upload_ceil")
					if up_ovr_rate_val and up_ovr_ceil_val:
						if if_name_val not in srv_cfg['interfaces']: srv_cfg['interfaces'][if_name_val] = {}
						srv_cfg['interfaces'][if_name_val]['upload'] = {'rate': up_ovr_rate_val, 'ceil': up_ovr_ceil_val}
				
				if 'download' in srv_cfg:
					ifb_name_for_override = self._get_macro_value(raw_macros, f"{self.IFACE_PREFIX}{if_key}_IFB", f"{srv_ctx} ifb for override {if_key}")
					if ifb_name_for_override:
						dl_ovr_rate_val = self._validate_rate_ceil(self._get_macro_value(raw_macros, f"{srv_prefix}OVERRIDE_{ifb_name_for_override.upper()}_DOWNLOAD_RATE", f"{srv_ctx} override {ifb_name_for_override} download_rate"), f"{srv_ctx} override {ifb_name_for_override} download_rate")
						if not dl_ovr_rate_val:
							dl_ovr_rate_val = self._validate_rate_ceil(self._get_macro_value(raw_macros, f"{srv_prefix}OVERRIDE_{if_key}_DOWNLOAD_RATE", f"{srv_ctx} override {if_key} download_rate (fallback)"), f"{srv_ctx} override {if_key} download_rate (fallback)")
						
						dl_ovr_ceil_val = self._validate_rate_ceil(self._get_macro_value(raw_macros, f"{srv_prefix}OVERRIDE_{ifb_name_for_override.upper()}_DOWNLOAD_CEIL", f"{srv_ctx} override {ifb_name_for_override} download_ceil"), f"{srv_ctx} override {ifb_name_for_override} download_ceil")
						if not dl_ovr_ceil_val:
							dl_ovr_ceil_val = self._validate_rate_ceil(self._get_macro_value(raw_macros, f"{srv_prefix}OVERRIDE_{if_key}_DOWNLOAD_CEIL", f"{srv_ctx} override {if_key} download_ceil (fallback)"), f"{srv_ctx} override {if_key} download_ceil (fallback)")

						if dl_ovr_rate_val and dl_ovr_ceil_val:
							if ifb_name_for_override not in srv_cfg['interfaces']: srv_cfg['interfaces'][ifb_name_for_override] = {}
							srv_cfg['interfaces'][ifb_name_for_override]['download'] = {'rate': dl_ovr_rate_val, 'ceil': dl_ovr_ceil_val}
			
			if not srv_cfg['interfaces']: del srv_cfg['interfaces']
			
			if 'upload' in srv_cfg or 'download' in srv_cfg:
				self.config['services'].append(srv_cfg)
			else:
				logger.warning(f"Servico {srv_key_from_list} (marca {mark_val:#04x}) nao tem configuracao de upload nem download valida apos parsing. Ignorando.")

		logger.info(f"Configuracao QoS lida dos macros: {len(self.config['interfaces'])} interfaces, {len(self.config['services'])} servicos.")
		return True

	def _get_config_interfaces(self):
		return self.config.get('interfaces', [])

	def _full_cleanup_attempt(self):
		logger.info("Tentando limpeza completa TC/IFBs...")
		interfaces = self._get_config_interfaces()
		if not interfaces: logger.warning("Nenhuma interface na config para cleanup TC/IFB.")
		else:
			logger.debug("Limpando qdiscs interfaces físicas...")
			for iface_cfg in interfaces:
				if isinstance(iface_cfg, dict) and 'name' in iface_cfg: self._cleanup_tc(iface_cfg['name'])
			logger.debug("Removendo interfaces IFB listadas...")
			known_ifbs = set()
			for iface_cfg in interfaces:
				 if isinstance(iface_cfg, dict) and 'ifb' in iface_cfg:
					 ifb_name = iface_cfg['ifb']
					 if ifb_name and ifb_name not in known_ifbs: self._cleanup_ifb(ifb_name); known_ifbs.add(ifb_name)
		self.managed_ifbs = {}; logger.info("Limpeza inicial TC/IFB completa.")

	def setup_tc(self):
		if not self.config: logger.error("Config não carregada para TC."); return False
		interfaces = self.config.get('interfaces', [])
		if not interfaces: logger.warning("Nenhuma interface definida para TC."); return True
		modules_needed = ['ifb', 'sch_htb', 'act_ctinfo']
		logger.info("Carregando módulos do kernel necessários...")
		for mod in modules_needed: self._run_command(['modprobe', mod], check=False, failure_ok=True)
		logger.info("Configurando TC...")
		tc_success = True
		for iface_cfg in interfaces:
			if not isinstance(iface_cfg, dict) or 'name' not in iface_cfg: logger.warning(f"Config de iface inválida: {iface_cfg}"); continue
			if not self._setup_iface(iface_cfg): tc_success = False
		if not tc_success: logger.error("Falha config TC para uma ou mais interfaces.")
		return tc_success

	def _setup_iface(self, iface_cfg):
		iface = iface_cfg['name']; ifb_name = iface_cfg.get('ifb')
		logger.info(f"Configurando TC para iface: {iface}" + (f" com IFB: {ifb_name}" if ifb_name else ""))
		if not Path(f"/sys/class/net/{iface}").exists(): logger.warning(f"Iface física {iface} não encontrada."); return True
		if not self._run_command(['ip', 'link', 'set', 'dev', iface, 'up'], check=False, failure_ok=True): logger.warning(f"Falha ao garantir que {iface} está UP.")
		if 'total_upload_bw' in iface_cfg and 'default_upload_class' in iface_cfg:
			if self._setup_shaping(iface, iface_cfg['total_upload_bw'], iface_cfg.get('default_upload_class'), 'upload'): self._apply_classes_and_filters(iface, 'upload')
			else: logger.error(f"Falha shaping upload (HTB) para {iface}."); return False
		else: logger.info(f"Shaping upload não config {iface} (faltam total_upload_bw/default_upload_class).")
		if ifb_name and 'total_download_bw' in iface_cfg and 'default_download_class' in iface_cfg:
			if not self._setup_ifb(iface, ifb_name): logger.error(f"Falha config IFB {ifb_name} p/ {iface}."); return True
			self.managed_ifbs[iface] = ifb_name
			if self._setup_shaping(ifb_name, iface_cfg['total_download_bw'], iface_cfg.get('default_download_class'), 'download'): self._apply_classes_and_filters(ifb_name, 'download')
			else: logger.error(f"Falha shaping download (HTB) para {ifb_name}."); return False
		elif ifb_name: logger.info(f"Shaping download não config {iface}/{ifb_name} (faltam total_download_bw/default_download_class).")
		return True

	def _setup_ifb(self, iface, ifb_name):
		logger.info(f"Configurando IFB {ifb_name} para {iface} (com ctinfo cpmark)")
		if not Path(f"/sys/class/net/{ifb_name}").exists():
			logger.info(f"IFB {ifb_name} não existe, criando...")
			if not self._run_command(['ip', 'link', 'add', ifb_name, 'type', 'ifb']): logger.error(f"Falha ao criar IFB {ifb_name}."); return False
			logger.info(f"IFB {ifb_name} criada.")
		else: logger.info(f"IFB {ifb_name} já existe.")
		if not self._run_command(['ip', 'link', 'set', 'dev', ifb_name, 'up']): logger.error(f"Falha ao ativar IFB {ifb_name}."); return False
		self._run_command(['tc', 'qdisc', 'del', 'dev', iface, 'ingress'], check=False, failure_ok=True)
		if not self._run_command(['tc', 'qdisc', 'add', 'dev', iface, 'handle', 'ffff:', 'ingress']): logger.error(f"Falha ao adicionar qdisc ingress em {iface}."); return False
		cmd_filter = ['tc', 'filter', 'replace', 'dev', iface, 'parent', 'ffff:', 'protocol', 'all', 'prio', '1', 'u32', 'match', 'u32', '0', '0', 'action', 'ctinfo', 'cpmark', 'action', 'mirred', 'egress', 'redirect', 'dev', ifb_name]
		if not self._run_command(cmd_filter):
			logger.error(f"Falha ao adicionar filtro redirect com ctinfo cpmark {iface}->{ifb_name}."); self._run_command(['tc', 'qdisc', 'del', 'dev', iface, 'ingress'], check=False, failure_ok=True); return False
		logger.info(f"Redirect {iface}->{ifb_name} com ctinfo cpmark configurado OK."); return True

	def _setup_shaping(self, iface, bandwidth, default_class, direction):
		logger.info(f"Configurando shaping HTB {direction} em {iface}...")
		if not Path(f"/sys/class/net/{iface}").exists(): logger.error(f"Interface {iface} não encontrada."); return False
		self._run_command(['tc', 'qdisc', 'del', 'dev', iface, 'root'], check=False, failure_ok=True); time.sleep(0.1)
		if bandwidth is None: logger.error(f"Largura de banda total ({direction}) não def."); return False
		logger.info(f"Aplicando HTB {direction} em {iface} (Banda: {bandwidth})")
		if not default_class or not all(k in default_class for k in ('id', 'rate', 'ceil')): logger.error(f"Classe default {direction} inválida."); return False
		try: default_minor_id = default_class['id'].split(':')[-1]; assert default_minor_id.isdigit()
		except Exception: logger.error(f"ID classe default {direction} inválido."); return False
		if not self._run_command(['tc', 'qdisc', 'add', 'dev', iface, 'root', 'handle', '1:', 'htb', 'default', default_minor_id]): logger.error(f"Falha add qdisc root HTB {direction}."); return False
		if not self._run_command(['tc', 'class', 'add', 'dev', iface, 'parent', '1:', 'classid', '1:1', 'htb', 'rate', bandwidth, 'ceil', bandwidth]): logger.error(f"Falha add classe raiz HTB {direction}."); self._run_command(['tc', 'qdisc', 'del', 'dev', iface, 'root'], check=False, failure_ok=True); return False
		default_class_prio = str(default_class.get('priority', 7)); default_class_id = default_class['id']
		if not self._run_command(['tc', 'class', 'add', 'dev', iface, 'parent', '1:1', 'classid', default_class_id, 'htb', 'rate', default_class['rate'], 'ceil', default_class['ceil'], 'prio', default_class_prio]): logger.error(f"Falha add classe default HTB {direction}."); self._run_command(['tc', 'qdisc', 'del', 'dev', iface, 'root'], check=False, failure_ok=True); return False
		logger.info(f"Classe default {direction} {default_class_id} config OK.")
		return True

	def _apply_classes_and_filters(self, iface, direction):
		logger.info(f"Aplicando classes/filtros de serviço {direction.upper()} em {iface}...")
		services = self.config.get('services', [])
		if not services: logger.info("Nenhuma classe de serviço definida."); return

		default_mark_hex = '0xff'
		default_class_id_for_filter = None
		for if_cfg_item in self.config.get('interfaces', []):
			if (direction == 'upload' and if_cfg_item.get('name') == iface) or \
			   (direction == 'download' and if_cfg_item.get('ifb') == iface):
				key = f'default_{direction}_class'
				if key in if_cfg_item and isinstance(if_cfg_item[key], dict):
					default_class_id_for_filter = if_cfg_item[key].get('id')
					break
		
		if not default_class_id_for_filter:
			logger.error(f"Não foi possível encontrar ID da classe default para {direction} em {iface}. Filtro default NÃO será adicionado.")
		
		for service in services:
			if not isinstance(service, dict): logger.warning(f"Def serviço inválida: {service}"); continue
			if 'mark' not in service: logger.warning(f"Serviço sem 'mark' ignorado: {service}"); continue

			if direction == 'upload':
				if 'upload' in service and isinstance(service['upload'], dict):
					self._add_upload_class_and_filter(iface, service)
			elif direction == 'download':
				if 'download' in service and isinstance(service['download'], dict):
					self._add_download_class_and_filter(iface, service)
		
		if default_class_id_for_filter:
			logger.info(f"Aplicando filtro default {direction} (marca {default_mark_hex} -> {default_class_id_for_filter}) em {iface}...")
			if not self._run_command(['tc', 'filter', 'replace', 'dev', iface, 'parent', '1:', 'protocol', 'ip', 'prio', '20',
									 'u32', 'match', 'mark', default_mark_hex, '0xffffffff', 'flowid', default_class_id_for_filter]):
				logger.error(f"Falha ao adicionar filtro default {direction} (mark {default_mark_hex}) em {iface}.")
			else:
				logger.info(f"Filtro default {direction} (mark {default_mark_hex} -> {default_class_id_for_filter}) config OK.")

	def _add_upload_class_and_filter(self, iface, service):
		try:
			base_upload_cfg = service['upload']; mark_int = service['mark']; mark_hex = hex(mark_int)
			# CORREÇÃO: Inicializar final_class_priority e final_filter_prio com os defaults do serviço
			final_cfg = base_upload_cfg.copy()
			final_class_priority = str(service.get('priority', 5))
			final_filter_prio = str(base_upload_cfg.get('filter_priority', 10))

			if 'interfaces' in service and iface in service['interfaces'] and isinstance(service['interfaces'][iface], dict) and 'upload' in service['interfaces'][iface] and isinstance(service['interfaces'][iface]['upload'], dict):
				override_cfg = service['interfaces'][iface]['upload']; logger.debug(f"Override upload m:{mark_hex} i:{iface} {override_cfg}"); final_cfg.update(override_cfg)
				# Atualizar se houver override específico
				final_class_priority = str(override_cfg.get('priority', final_class_priority)) # Usa o já definido se não houver no override
				final_filter_prio = str(override_cfg.get('filter_priority', final_filter_prio)) # Usa o já definido se não houver no override

			if not all(k in final_cfg for k in ('class_id_suffix', 'rate', 'ceil')): logger.error(f"Cfg upload incompleta m:{mark_hex} i:{iface}"); return
			class_id_suffix = final_cfg['class_id_suffix']
			if not isinstance(class_id_suffix, (str, int)) or not str(class_id_suffix).isdigit(): logger.error(f"class_id_suffix upload inválido m:{mark_hex} i:{iface}"); return
			class_id = f"1:{class_id_suffix}"
			logger.info(f"Config classe UPLOAD {class_id} m:{mark_hex} i:{iface} (r:{final_cfg['rate']} c:{final_cfg['ceil']} p:{final_class_priority})")
			if not self._run_command(['tc', 'class', 'replace', 'dev', iface, 'parent', '1:1', 'classid', class_id, 'htb', 'rate', final_cfg['rate'], 'ceil', final_cfg['ceil'], 'prio', final_class_priority]): logger.error(f"Falha classe upload {class_id}."); return
			if not self._run_command(['tc', 'filter', 'replace', 'dev', iface, 'parent', '1:', 'protocol', 'ip', 'prio', final_filter_prio, 'u32', 'match', 'mark', mark_hex, '0xffffffff', 'flowid', class_id]): logger.error(f"Falha filtro upload m:{mark_hex}.")
			else: logger.info(f"Filtro upload (m:{mark_hex} -> {class_id}, prio:{final_filter_prio}) OK.")
		except KeyError as e: logger.error(f"Erro cfg serviço upload m:{service.get('mark', 'N/A')} i:{iface}: Chave {e}")
		except Exception as e: logger.error(f"Erro inesperado upload m:{service.get('mark', 'N/A')} i:{iface}: {e}", exc_info=True)

	def _add_download_class_and_filter(self, ifb_name, service):
		try:
			mark_int = service['mark']; mark_hex = hex(mark_int); base_download_cfg = service['download']
			# CORREÇÃO: Inicializar final_class_priority e final_filter_prio com os defaults do serviço
			final_cfg = base_download_cfg.copy()
			final_class_priority = str(service.get('priority', 5))
			final_filter_prio = str(base_download_cfg.get('filter_priority', 10)) # Default do serviço para download

			if 'interfaces' in service and ifb_name in service['interfaces'] and isinstance(service['interfaces'][ifb_name], dict) and 'download' in service['interfaces'][ifb_name] and isinstance(service['interfaces'][ifb_name]['download'], dict):
				override_cfg = service['interfaces'][ifb_name]['download']; logger.debug(f"Override download (connmark) m:{mark_hex} i IFB {ifb_name}: {override_cfg}"); final_cfg.update(override_cfg)
				# Atualizar se houver override específico
				final_class_priority = str(override_cfg.get('priority', final_class_priority))
				final_filter_prio = str(override_cfg.get('filter_priority', final_filter_prio))

			if not all(k in final_cfg for k in ('class_id_suffix', 'rate', 'ceil')): logger.error(f"Cfg download incompleta (connmark) m:{mark_hex} i:{ifb_name}"); return
			class_id_suffix = final_cfg['class_id_suffix']
			if not isinstance(class_id_suffix, (str, int)) or not str(class_id_suffix).isdigit(): logger.error(f"class_id_suffix download inválido (connmark) m:{mark_hex} i:{ifb_name}"); return
			class_id = f"1:{class_id_suffix}"
			logger.info(f"Config classe DOWNLOAD {class_id} (connmark m:{mark_hex}) i:{ifb_name} (r:{final_cfg['rate']} c:{final_cfg['ceil']} p:{final_class_priority})")
			if not self._run_command(['tc', 'class', 'replace', 'dev', ifb_name, 'parent', '1:1', 'classid', class_id, 'htb', 'rate', final_cfg['rate'], 'ceil', final_cfg['ceil'], 'prio', final_class_priority]): logger.error(f"Falha classe download {class_id} (connmark)."); return
			cmd_filter = ['tc', 'filter', 'replace', 'dev', ifb_name, 'parent', '1:', 'protocol', 'ip', 'prio', final_filter_prio, 'u32', 'match', 'mark', mark_hex, '0xffffffff', 'flowid', class_id]
			if not self._run_command(cmd_filter): logger.error(f"Falha filtro download (match mark {mark_hex}) -> {class_id} i:{ifb_name}.")
			else: logger.info(f"Filtro download (mark {mark_hex} -> {class_id}, prio: {final_filter_prio}) OK i:{ifb_name}.")
		except KeyError as e: logger.error(f"Erro cfg serviço download (connmark) m:{service.get('mark','N/A')} i:{ifb_name}: Chave {e}")
		except Exception as e: logger.error(f"Erro inesperado download (connmark) m:{service.get('mark','N/A')} i:{ifb_name}: {e}", exc_info=True)

	def _cleanup_tc(self, iface):
		if Path(f"/sys/class/net/{iface}").exists():
			logger.info(f"Limpando qdiscs root/ingress em {iface}")
			self._run_command(['tc', 'qdisc', 'del', 'dev', iface, 'root'], check=False, failure_ok=True)
			self._run_command(['tc', 'qdisc', 'del', 'dev', iface, 'ingress'], check=False, failure_ok=True)
		else: logger.debug(f"Interface {iface} não encontrada para cleanup TC.")

	def _cleanup_ifb(self, ifb_name):
		if Path(f"/sys/class/net/{ifb_name}").exists():
			logger.info(f"Removendo IFB {ifb_name}")
			self._run_command(['tc', 'qdisc', 'del', 'dev', ifb_name, 'root'], check=False, failure_ok=True)
			self._run_command(['tc', 'qdisc', 'del', 'dev', ifb_name, 'ingress'], check=False, failure_ok=True)
			self._run_command(['ip', 'link', 'set', 'dev', ifb_name, 'down'], check=False, failure_ok=True)
			if self._run_command(['ip', 'link', 'del', 'dev', ifb_name], check=False, failure_ok=True):
				time.sleep(0.1)
				if not Path(f"/sys/class/net/{ifb_name}").exists(): logger.info(f"IFB {ifb_name} removida com sucesso.")
				else: logger.warning(f"Comando 'ip link del {ifb_name}' executado, mas a interface ainda existe.")
			else: logger.warning(f"Comando 'ip link del {ifb_name}' falhou.")
		else: logger.debug(f"IFB {ifb_name} não encontrada para remoção.")

	def start(self):
		logger.info("Iniciando configuração QoS (Macros Foomuuri)...")
		try:
			if not self._parse_macros_from_foomuuri_conf():
				logger.error("Falha ao ler configuração dos macros. Abortando.")
				return False
			self._full_cleanup_attempt()
			if not self.setup_tc():
				raise Exception("Falha na configuração do TC (Macros Foomuuri).")
			logger.info("Configuração QoS (Macros Foomuuri) APLICADA.")
			return True
		except FileNotFoundError:
			logger.error("ERRO FATAL: Ficheiro de configuração Foomuuri não encontrado.")
			return False
		except Exception as e:
			logger.error(f"ERRO FATAL durante a config QoS (Macros Foomuuri): {e}", exc_info=True)
			logger.info("Tentando limpar config TC/IFB devido a erro no start..."); self.stop(); return False

	def stop(self):
		logger.info("Parando configuração QoS (Macros Foomuuri)...")
		self._full_cleanup_attempt()
		logger.info("Limpeza QoS (Macros Foomuuri) via stop concluída."); return True

def main():
	parser = argparse.ArgumentParser(description="Motor de QoS para Foomuuri (Lendo Macros do .conf)")
	parser.add_argument('--start', action='store_true', help="Aplica a configuração QoS")
	parser.add_argument('--stop', action='store_true', help="Remove a configuração QoS")
	parser.add_argument('--config-file', default="/etc/foomuuri/foomuuri.conf", help="Caminho para o ficheiro foomuuri.conf")
	args = parser.parse_args()
	if not args.start and not args.stop: parser.print_help(); sys.exit(1)
	if os.geteuid() != 0: logger.error("Executar como root."); print("failed - run as root", file=sys.stderr); sys.exit(1)
	engine = QoSEngineMacroParserValidated(foomuuri_config_path=args.config_file) # Nome da classe e argumento corrigidos
	success = False
	try:
		if args.start: success = engine.start()
		elif args.stop: success = engine.stop()
		if success: print("success"); sys.exit(0)
		else: print("failed"); sys.exit(1)
	except Exception as e: logger.error(f"Erro fatal main: {e}", exc_info=True); print(f"failed - {e}", file=sys.stderr); sys.exit(1)

if __name__ == "__main__":
	main()
