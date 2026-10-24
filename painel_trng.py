#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
painel_trng.py - tela para rodar o TRNG com lampadas de lava (TCC)

Coloque este arquivo na MESMA pasta do trng_lava.py e abra com duplo clique
em "Abrir painel.bat" (ou:  python painel_trng.py).

  Botoes azuis   passos do trng_lava.py, no Windows (camera, geracao, testes)
  Botoes roxos   ferramentas do NIST que rodam no Linux, chamadas pelo WSL:
                   SP 800-90B  ea_non_iid, ea_restart, ea_conditioning
                   SP 800-22   assess (sts-2.1.2) com os testes do recorte do TCC

A aba "WSL e NIST" instala e compila as ferramentas do NIST com um clique.
O painel so usa a biblioteca padrao do Python (tkinter). O trng_lava.py
continua precisando de numpy e opencv-python (a mesma aba instala).
Configuracoes e resultados ficam em painel_trng.json, nesta pasta.
"""

import codecs
import functools
import glob
import json
import math
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

PASTA = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(PASTA, "trng_lava.py")
ARQ_PAINEL = os.path.join(PASTA, "painel_trng.json")
NO_WINDOWS = os.name == "nt"
SEM_JANELA = getattr(subprocess, "CREATE_NO_WINDOW", 0)

URL_90B = "https://github.com/usnistgov/SP800-90B_EntropyAssessment.git"
URL_STS = "https://csrc.nist.gov/CSRC/media/Projects/Random-Bit-Generation/documents/sts-2_1_2.zip"
PACOTES_APT = ("build-essential git wget curl unzip libbz2-dev libdivsufsort-dev libjsoncpp-dev "
               "libssl-dev libmpfr-dev libgmp-dev")
PASTAS_STS = ("Frequency BlockFrequency Runs LongestRun Rank FFT NonOverlappingTemplate OverlappingTemplate "
              "Universal LinearComplexity Serial ApproximateEntropy CumulativeSums RandomExcursions "
              "RandomExcursionsVariant")
# testes do menu do assess: 01 Frequency, 02 Block Frequency, 04 Runs (recorte do TCC)
SELECAO_STS = "110100000000000"
N_BITS = 1_000_000

FONTES = [("bruto", "Lâmpadas fluindo", "a medição principal"),
          ("controle_tampada", "Controle: lente tampada", "tampe a lente antes de gravar"),
          ("controle_cera_parada", "Controle: cera parada", "lâmpadas acesas, cera ainda sólida")]
NOME_FONTE = {k: n for k, n, _ in FONTES}
ORDENS = ["horizontal", "vertical", "temporal"]

PADRAO = {"camera": 0, "fonte": "bruto", "h": "", "h_origem": "", "h_nist": None,
          "margem": 2.0, "sequencias": 100, "saida": "saida_trng.bin", "arquivo_teste": "saida_trng.bin",
          "distro": "", "dir_90b": "~/SP800-90B_EntropyAssessment/cpp", "dir_sts": "~/sts-2.1.2",
          "resultados": {"non_iid": {}, "restart": None, "conditioning": None, "testes": {}}}

COR = dict(fundo="#f4f6fb", lateral="#e8ecf4", card="#ffffff", borda="#d9dee8", texto="#1f2937",
           suave="#5b6475", azul="#2563eb", azul2="#1d4ed8", roxo="#7c3aed", roxo2="#6d28d9",
           cinza="#e5e7eb", cinza2="#d1d5db", vermelho="#dc2626", vermelho2="#b91c1c",
           desligado="#c9ced9", verde="#15803d", erro="#b91c1c", ambar="#b45309",
           log_fundo="#0f172a", log_texto="#dbe3ef")

RE_QUEBRA = re.compile(r"(\r\n|\r|\n)")
RE_LINHA_TRNG = re.compile(r"^\s*((?:\d+\s+){10})(----|[0-9.]+)\s*\*?\s+(\d+)\s*/\s*(\d+)\s*\*?\s+(.+?)\s+(ok|REPROVADO.*?)\s*$")
RE_LINHA_STS = re.compile(r"^\s*((?:\d+\s+){10})(----|[0-9.]+)\s*(\*?)\s+(\d+)\s*/\s*(\d+)\s*(\*?)\s+(\S+)\s*$")


# =============================================================================
# Funcoes auxiliares (sem interface)
# =============================================================================

def carregar_painel():
    cfg = json.loads(json.dumps(PADRAO))
    try:
        with open(ARQ_PAINEL, encoding="utf-8") as f:
            dados = json.load(f)
        res = dados.pop("resultados", {}) or {}
        cfg.update(dados)
        for k, v in res.items():
            if v is not None:
                cfg["resultados"][k] = v
    except (OSError, ValueError):
        pass
    return cfg


def wsl_exe():
    """wsl.exe; num Python de 32 bits o System32 e redirecionado, entao usa o Sysnative."""
    if NO_WINDOWS:
        sn = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Sysnative", "wsl.exe")
        if os.path.exists(sn):
            return sn
    return "wsl.exe"


def canonico(nome):
    """'Block Frequency (M=128)' e 'BlockFrequency' -> 'blockfrequency'."""
    return re.sub(r"[^a-z]", "", nome.lower().split("(")[0])


def ler_config_json():
    try:
        with open(os.path.join(PASTA, "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def ler_json(caminho):
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def python_filho():
    """Python que roda o trng_lava.py: o mesmo do painel (python.exe, nao pythonw.exe)."""
    exe = sys.executable or "python"
    base = os.path.basename(exe)
    if base.lower().startswith("pythonw"):
        alt = os.path.join(os.path.dirname(exe), "python" + base[7:])
        if os.path.exists(alt):
            return alt
    return exe


def sq(s):
    """Aspas simples do bash, sem aspas duplas (passam intactas pelo wsl.exe)."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def dir_bash(d):
    d = (d or "").strip().rstrip("/") or "~"
    if d == "~":
        return "~"
    if d.startswith("~/"):
        return "~/" + sq(d[2:])
    return sq(d)


def fmt(n):
    return f"{int(n):,}".replace(",", ".")


def para_float(s):
    try:
        return float(str(s).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def tamanho(nome):
    try:
        return os.path.getsize(os.path.join(PASTA, nome))
    except OSError:
        return 0


def ler_h(texto, k):
    """Resultado do ea_non_iid: H_original (1 bit) ou min(H_original, k X H_bitstring)."""
    if k > 1:
        m = re.findall(r"min\(H_original,\s*\d+\s*X\s*H_bitstring\):\s*([0-9.]+)", texto)
        if m:
            return float(m[-1])
    m = re.findall(r"H_original:\s*([0-9.]+)", texto)
    return float(m[-1]) if m else None


def ler_restart(texto):
    r = {}
    for nome in ("H_r", "H_c", "H_I"):
        m = re.findall(rf"^{nome}:\s*([0-9.]+)", texto, re.M)
        if m:
            r[nome] = float(m[-1])
    m = re.findall(r"min\(H_r, H_c, H_I\):\s*([0-9.]+)", texto)
    if m:
        r["min"] = float(m[-1])
    r["ok"] = "Validation Test Passed" in texto and "Failed" not in texto and "min" in r
    falha = [l.strip(" *") for l in texto.splitlines() if "Failed" in l]
    r["motivo"] = falha[-1] if falha else ""
    return r


def calc_condicionamento(h, margem, k):
    """Mesma conta do 'gerar' do trng_lava.py."""
    n_s = math.ceil(margem * 256 / h)
    if k == 1:
        n_s = -(-n_s // 8) * 8
    n_in = n_s if k == 1 else 8 * n_s
    return n_s, n_in, n_s * h


def ler_tabela_trng(texto):
    linhas = []
    for l in texto.splitlines():
        m = RE_LINHA_TRNG.match(l)
        if m:
            linhas.append({"teste": m.group(5).strip(), "prop": f"{m.group(3)}/{m.group(4)}",
                           "unif": m.group(2), "ok": m.group(6) == "ok"})
    return linhas


def ler_tabela_sts(texto):
    linhas = []
    for l in texto.splitlines():
        m = RE_LINHA_STS.match(l)
        if m:
            linhas.append({"teste": m.group(7), "prop": f"{m.group(4)}/{m.group(5)}",
                           "unif": m.group(2), "ok": not (m.group(3) or m.group(6))})
    return linhas


def ler_verificacao(texto):
    r = {"wsl": False, "kernel": "", "tem": [], "faltam": []}
    for l in texto.splitlines():
        l = l.strip()
        if l.startswith("WSL_OK"):
            r["wsl"], r["kernel"] = True, l[6:].strip()
        elif l.startswith("TEM "):
            r["tem"].append(l[4:].strip())
        elif l.startswith("FALTA "):
            r["faltam"].append(l[6:].strip())
    return r


def decodificar(b):
    if b.count(b"\x00") > len(b) // 4:           # mensagens do wsl.exe vem em UTF-16
        return b.decode("utf-16-le", errors="replace").replace("﻿", "")
    return b.decode("utf-8", errors="replace").replace("\x00", "")


def prog_bruto(l):
    m = re.search(r"amostras ([\d.]+)/([\d.]+)\s+temporal (\d+)/(\d+)", l)
    if m:
        a, b, t, T = (int(x.replace(".", "")) for x in m.groups())
        return 0.5 * (a / max(b, 1) + t / max(T, 1))
    return None


def prog_fracao(padrao):
    rx = re.compile(padrao)

    def f(l):
        m = rx.search(l)
        return int(m.group(1)) / max(1, int(m.group(2))) if m else None
    return f


def prog_pct(l):
    m = re.match(r"\s*([\d.]+)%", l)
    return float(m.group(1)) / 100 if m else None


# =============================================================================
# Painel
# =============================================================================

class App:
    PASSOS = [("calibrar", "1", "Calibrar câmera"), ("bruto", "2", "Dados brutos"),
              ("reinicio", "3", "Reinícios"), ("gerar", "4", "Gerar saída"),
              ("testar", "5", "Testes"), ("wsl", "⚙", "WSL e NIST")]

    def __init__(self, raiz):
        self.raiz = raiz
        self.cfg = carregar_painel()
        self.fila = queue.Queue()
        self.proc = None
        self.ocupado = False
        self.parar_pedido = False
        self.lote, self.lote_total, self.lote_ok, self.lote_fim = [], 0, True, None
        self.tarefa = None
        self.cr = False
        self.botoes = []
        self.paginas, self.itens, self.rolagens = {}, {}, {}
        self.atual = None
        self.verif = None              # resultado da verificacao do WSL
        self.modulos = None            # numpy / cv2 no Python do Windows
        self._fontes()
        self._estilos()
        self._variaveis()
        self._montar()
        self._auto_bits()
        self.mostrar("calibrar")
        self.raiz.after(40, self._ler_fila)
        self.raiz.protocol("WM_DELETE_WINDOW", self.fechar)
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.raiz.bind_all(ev, self._roda, add="+")
        self.raiz.after(400, self.checagens_iniciais)
        if not os.path.exists(SCRIPT):
            self.raiz.after(600, lambda: messagebox.showwarning(
                "trng_lava.py não encontrado",
                f"Coloque o painel na mesma pasta do trng_lava.py.\n\nPasta atual:\n{PASTA}"))

    # ------------------------------------------------------------------ visual
    def _fontes(self):
        fam = "Segoe UI" if NO_WINDOWS else tkfont.nametofont("TkDefaultFont").actual("family")
        mono = "Consolas" if NO_WINDOWS else "DejaVu Sans Mono"
        self.f_normal = tkfont.Font(family=fam, size=10)
        self.f_negrito = tkfont.Font(family=fam, size=10, weight="bold")
        self.f_peq = tkfont.Font(family=fam, size=9)
        self.f_sub = tkfont.Font(family=fam, size=11, weight="bold")
        self.f_titulo = tkfont.Font(family=fam, size=16, weight="bold")
        self.f_grande = tkfont.Font(family=fam, size=12, weight="bold")
        self.f_mono = tkfont.Font(family=mono, size=9)
        for nome in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            tkfont.nametofont(nome).configure(family=fam, size=10)

    def _estilos(self):
        s = ttk.Style(self.raiz)
        s.theme_use("clam")
        card = COR["card"]
        s.configure(".", font=self.f_normal)
        for nome, bg in (("Card.TRadiobutton", card), ("Card.TCheckbutton", card),
                         ("Fundo.TCheckbutton", COR["fundo"])):
            s.configure(nome, background=bg, foreground=COR["texto"], focuscolor=bg)
            s.map(nome, background=[("active", bg)])
        s.configure("TEntry", fieldbackground="white", bordercolor=COR["borda"], padding=4)
        s.configure("TSpinbox", fieldbackground="white", bordercolor=COR["borda"], padding=3, arrowsize=12)
        s.configure("TCombobox", fieldbackground="white", bordercolor=COR["borda"], padding=3, arrowsize=14)
        alt = self.f_normal.metrics("linespace") + 10
        s.configure("Treeview", background="white", fieldbackground="white", foreground=COR["texto"],
                    rowheight=alt, bordercolor=COR["borda"], font=self.f_normal)
        s.configure("Treeview.Heading", font=self.f_negrito, background=COR["lateral"],
                    foreground=COR["texto"], relief="flat", padding=5)
        s.map("Treeview", background=[("selected", "#dbe6ff")], foreground=[("selected", COR["texto"])])
        s.configure("Leve.Vertical.TScrollbar", background=COR["cinza"], troughcolor=COR["fundo"],
                    bordercolor=COR["fundo"], arrowcolor=COR["suave"], lightcolor=COR["cinza"],
                    darkcolor=COR["cinza"])
        s.configure("Azul.Horizontal.TProgressbar", troughcolor=COR["cinza"], background=COR["azul"],
                    bordercolor=COR["cinza"], lightcolor=COR["azul"], darkcolor=COR["azul"], thickness=10)

    def _variaveis(self):
        c = self.cfg
        self.v_camera = tk.StringVar(value=str(c.get("camera", 0)))
        self.v_fonte = tk.StringVar(value=c.get("fonte", "bruto"))
        self.v_do_zero = tk.BooleanVar(value=False)
        self.v_h = tk.StringVar(value=c.get("h", ""))
        self.v_h_origem = tk.StringVar(value=c.get("h_origem", ""))
        self.v_margem = tk.StringVar(value=str(c.get("margem", 2.0)))
        self.v_seq = tk.StringVar(value=str(c.get("sequencias", 100)))
        self.v_saida = tk.StringVar(value=c.get("saida", "saida_trng.bin"))
        self.v_arquivo = tk.StringVar(value=c.get("arquivo_teste", "saida_trng.bin"))
        self.v_bits = tk.BooleanVar(value=False)
        self.v_distro = tk.StringVar(value=c.get("distro") or "(padrão)")
        self.v_90b = tk.StringVar(value=c.get("dir_90b") or PADRAO["dir_90b"])
        self.v_sts = tk.StringVar(value=c.get("dir_sts") or PADRAO["dir_sts"])
        self.v_status = tk.StringVar(value="Pronto.")
        for v in (self.v_h, self.v_margem, self.v_seq, self.v_saida):
            v.trace_add("write", lambda *a: self.atualizar_gerar())
        self.v_arquivo.trace_add("write", lambda *a: self._auto_bits())
        self._definindo_h = False
        self.v_h.trace_add("write", lambda *a: self._definindo_h or self.v_h_origem.set("digitado à mão"))
        self.v_fonte.trace_add("write", lambda *a: self.salvar())

    def _quebra(self, lbl, margem=4):
        lbl.bind("<Configure>", lambda e: lbl.configure(wraplength=max(120, e.width - margem)))
        return lbl

    def label(self, pai, texto="", fonte=None, cor=None, bg=None, **kw):
        return tk.Label(pai, text=texto, font=fonte or self.f_normal, fg=cor or COR["texto"],
                        bg=bg or COR["card"], anchor="w", justify="left", **kw)

    def botao(self, pai, texto, comando, tipo="azul", acao=True, pequeno=False):
        cores = {"azul": (COR["azul"], "white", COR["azul2"]), "roxo": (COR["roxo"], "white", COR["roxo2"]),
                 "cinza": (COR["cinza"], COR["texto"], COR["cinza2"]),
                 "vermelho": (COR["vermelho"], "white", COR["vermelho2"])}
        bg, fg, hover = cores[tipo]
        b = tk.Button(pai, text=texto, command=comando, bg=bg, fg=fg, activebackground=hover,
                      activeforeground=fg, disabledforeground="#f3f4f6" if tipo != "cinza" else "#9ca3af",
                      relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                      font=self.f_peq if pequeno else self.f_negrito,
                      padx=10 if pequeno else 16, pady=4 if pequeno else 8)
        b.cor, b.hover = bg, hover
        b.bind("<Enter>", lambda e: str(b["state"]) == "normal" and b.configure(bg=b.hover))
        b.bind("<Leave>", lambda e: str(b["state"]) == "normal" and b.configure(bg=b.cor))
        if acao:
            self.botoes.append(b)
        return b

    def card(self, pai, titulo=None, nota=None, faixa=None, etiqueta=None):
        f = tk.Frame(pai, bg=COR["card"], highlightbackground=COR["borda"], highlightthickness=1)
        if faixa:
            tk.Frame(f, bg=faixa, height=3).pack(fill="x")
        corpo = tk.Frame(f, bg=COR["card"], padx=16, pady=12)
        corpo.pack(fill="both", expand=True)
        if titulo:
            topo = tk.Frame(corpo, bg=COR["card"])
            topo.pack(fill="x")
            self.label(topo, titulo, self.f_sub).pack(side="left")
            if etiqueta:
                tk.Label(topo, text=etiqueta, font=self.f_peq, bg="#ede9fe" if faixa == COR["roxo"] else "#dbeafe",
                         fg=faixa or COR["azul"], padx=6).pack(side="left", padx=8)
        if nota:
            self._quebra(self.label(corpo, nota, self.f_peq, COR["suave"])).pack(fill="x", pady=(3, 8))
        return f, corpo

    def linha2(self, pai, expandir=False):
        f = tk.Frame(pai, bg=COR["fundo"])
        f.pack(fill="both" if expandir else "x", expand=expandir, pady=(0, 12))
        f.columnconfigure(0, weight=1, uniform="col")
        f.columnconfigure(1, weight=1, uniform="col")
        f.rowconfigure(0, weight=1)
        return f

    def em_coluna(self, card, col):
        card.grid(row=0, column=col, sticky="nsew", padx=(0, 6) if col == 0 else (6, 0))

    def pagina(self, chave, titulo, texto):
        """Pagina com rolagem: nada fica cortado em telas pequenas ou com zoom do Windows."""
        caixa = tk.Frame(self.conteudo, bg=COR["fundo"])
        caixa.grid(row=0, column=0, sticky="nsew")
        cv = tk.Canvas(caixa, bg=COR["fundo"], highlightthickness=0, bd=0, yscrollincrement=20)
        sb = ttk.Scrollbar(caixa, orient="vertical", command=cv.yview, style="Leve.Vertical.TScrollbar")
        cv.pack(side="left", fill="both", expand=True)
        p = tk.Frame(cv, bg=COR["fundo"], padx=22, pady=14)
        jan = cv.create_window(0, 0, window=p, anchor="nw")

        def rolagem(a, b):
            sb.set(a, b)
            precisa = float(a) > 0 or float(b) < 1
            if precisa and not sb.winfo_ismapped():
                sb.pack(side="right", fill="y", before=cv)
            elif not precisa and sb.winfo_ismapped() and p.winfo_reqheight() < cv.winfo_height() - 40:
                sb.pack_forget()
        cv.configure(yscrollcommand=rolagem)
        p.bind("<Configure>", lambda e: cv.configure(scrollregion=(0, 0, e.width, e.height)))
        cv.bind("<Configure>", lambda e: cv.itemconfigure(jan, width=e.width))
        tk.Label(p, text=titulo, font=self.f_titulo, bg=COR["fundo"], fg=COR["texto"], anchor="w").pack(fill="x")
        self._quebra(self.label(p, texto, self.f_normal, COR["suave"], bg=COR["fundo"])).pack(fill="x", pady=(2, 10))
        self.paginas[chave] = caixa
        self.rolagens[chave] = cv
        return p

    def tabela(self, pai, colunas, larguras, altura=5):
        f = tk.Frame(pai, bg=COR["card"])
        ids = [f"c{i}" for i in range(len(colunas))]
        t = ttk.Treeview(f, columns=ids, show="headings", height=altura, selectmode="browse")
        for c, nome, w in zip(ids, colunas, larguras):
            t.heading(c, text=nome, anchor="w")
            t.column(c, width=w, minwidth=60, anchor="w", stretch=True)
        t.tag_configure("ok", foreground=COR["verde"])
        t.tag_configure("falha", foreground=COR["erro"])
        t.tag_configure("forte", font=self.f_negrito)
        t.tag_configure("pendente", foreground=COR["suave"])
        sb = ttk.Scrollbar(f, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        return f, t

    # ------------------------------------------------------------------ montagem
    def _montar(self):
        r = self.raiz
        r.configure(bg=COR["fundo"])
        topo = tk.Frame(r, bg=COR["card"], highlightbackground=COR["borda"], highlightthickness=1)
        topo.pack(fill="x")
        dir_ = tk.Frame(topo, bg=COR["card"], padx=18)
        dir_.pack(side="right")
        esq = tk.Frame(topo, bg=COR["card"], padx=18, pady=8)
        esq.pack(side="left", fill="x", expand=True)
        self.label(esq, "TRNG com lâmpadas de lava", self.f_grande).pack(anchor="w")
        self.label(esq, f"Pasta: {PASTA}", self.f_peq, COR["suave"]).pack(anchor="w")
        self.l_wsl_topo = self.label(dir_, "WSL: verificando…", self.f_peq, COR["suave"])
        self.l_wsl_topo.pack(side="left", padx=(0, 12))
        self.botao(dir_, "Abrir pasta", self.abrir_pasta, "cinza", acao=False, pequeno=True).pack(side="left")

        self.paned = tk.PanedWindow(r, orient="vertical", sashwidth=7, bg=COR["borda"], bd=0,
                                    sashrelief="flat", showhandle=False)
        self.paned.pack(fill="both", expand=True)
        meio = tk.Frame(self.paned, bg=COR["fundo"])
        baixo = tk.Frame(self.paned, bg=COR["card"])
        self.paned.add(meio, minsize=380, stretch="always")
        self.paned.add(baixo, minsize=120, height=210, stretch="never")

        larg = max(210, self.f_negrito.measure("Calibrar câmera") + 120)
        lateral = tk.Frame(meio, bg=COR["lateral"], width=larg)
        lateral.pack(side="left", fill="y")
        lateral.pack_propagate(False)
        tk.Frame(lateral, bg=COR["lateral"], height=12).pack()
        for chave, num, texto in self.PASSOS:
            if chave == "wsl":
                tk.Frame(lateral, bg=COR["borda"], height=1).pack(fill="x", padx=14, pady=10)
            self._item_lateral(lateral, chave, num, texto)
        legenda = tk.Frame(lateral, bg=COR["lateral"], padx=16, pady=14)
        legenda.pack(side="bottom", fill="x")
        for cor, txt in ((COR["azul"], "no Windows (câmera)"), (COR["roxo"], "no NIST, pelo WSL")):
            l = tk.Frame(legenda, bg=COR["lateral"])
            l.pack(anchor="w", pady=1)
            tk.Frame(l, bg=cor, width=10, height=10).pack(side="left", padx=(0, 6))
            tk.Label(l, text=txt, bg=COR["lateral"], fg=COR["suave"], font=self.f_peq).pack(side="left")

        self.conteudo = tk.Frame(meio, bg=COR["fundo"])
        self.conteudo.pack(side="left", fill="both", expand=True)
        self.conteudo.rowconfigure(0, weight=1)
        self.conteudo.columnconfigure(0, weight=1)
        self._pagina_calibrar()
        self._pagina_bruto()
        self._pagina_reinicio()
        self._pagina_gerar()
        self._pagina_testar()
        self._pagina_wsl()
        self._area_log(baixo)
        self.raiz.after(200, self._ajustar_divisoria)

    def _ajustar_divisoria(self):
        alt = self.paned.winfo_height()
        if alt > 1:
            self.paned.sash_place(0, 0, alt - int(min(200, max(130, alt * 0.24))))

    def _item_lateral(self, pai, chave, num, texto):
        f = tk.Frame(pai, bg=COR["lateral"], cursor="hand2")
        f.pack(fill="x", padx=8, pady=1)
        barra = tk.Frame(f, bg=COR["lateral"], width=4)
        barra.pack(side="left", fill="y")
        n = tk.Label(f, text=num, width=2, font=self.f_negrito, bg=COR["cinza2"], fg="white")
        n.pack(side="left", padx=(8, 8), pady=8)
        t = tk.Label(f, text=texto, font=self.f_normal, bg=COR["lateral"], fg=COR["texto"], anchor="w")
        t.pack(side="left", fill="x", expand=True)
        m = tk.Label(f, text="", font=self.f_negrito, bg=COR["lateral"], fg=COR["verde"], width=2)
        m.pack(side="right", padx=(0, 8))
        for w in (f, barra, n, t, m):
            w.bind("<Button-1>", lambda e, c=chave: self.mostrar(c))
        self.itens[chave] = {"f": f, "barra": barra, "num": n, "txt": t, "marca": m}

    def _roda(self, e):
        w = e.widget
        if not isinstance(w, tk.Misc) or isinstance(w, (ttk.Treeview, tk.Text, ttk.Combobox, ttk.Spinbox)):
            return
        caixa, cv = self.paginas.get(self.atual), self.rolagens.get(self.atual)
        if cv is None or not str(w).startswith(str(caixa)):
            return
        if cv.yview() != (0.0, 1.0):
            cv.yview_scroll(-3 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 3, "units")

    def mostrar(self, chave):
        self.atual = chave
        self.paginas[chave].tkraise()
        for c, it in self.itens.items():
            sel = c == chave
            bg = COR["card"] if sel else COR["lateral"]
            for w in ("f", "txt", "marca"):
                it[w].configure(bg=bg)
            it["barra"].configure(bg=COR["azul"] if sel else bg)
            it["num"].configure(bg=COR["azul"] if sel else COR["cinza2"])
            it["txt"].configure(font=self.f_negrito if sel else self.f_normal)
        if chave == "testar":
            self.atualizar_lista_arquivos()
        self.atualizar_tudo()

    # ---------- pagina 1
    def _pagina_calibrar(self):
        p = self.pagina("calibrar", "1 · Calibrar a câmera",
                        "Abre a câmera ao vivo para marcar as 4 lâmpadas e acertar a exposição. "
                        "Tudo fica salvo em config.json e vale para todos os outros passos.")
        lin = self.linha2(p)
        c, b = self.card(lin, "Na câmera", faixa=COR["azul"], etiqueta="Windows")
        self.em_coluna(c, 0)
        f = tk.Frame(b, bg=COR["card"])
        f.pack(fill="x", pady=(6, 10))
        self.label(f, "Câmera nº").pack(side="left")
        ttk.Spinbox(f, from_=0, to=9, width=4, textvariable=self.v_camera).pack(side="left", padx=(6, 14))
        self.botao(f, "Abrir câmera e calibrar", self.acao_calibrar).pack(side="left")
        self._quebra(self.label(b, "Meta: “sat” e “esc” em 0% e “LSB=ant” perto de 0.50 em todas as lâmpadas. "
                                   "Se a câmera não abrir, tente o número 1 ou 2.", self.f_peq, COR["suave"])
                     ).pack(fill="x")
        c, b = self.card(lin, "Teclas na janela da câmera",
                         nota="Clique na janela da câmera antes de apertar as teclas.")
        self.em_coluna(c, 1)
        g = tk.Frame(b, bg=COR["card"])
        g.pack(fill="x")
        g.columnconfigure(1, weight=1)
        teclas = [("r", "marcar as lâmpadas: um retângulo por lâmpada, ENTER depois de cada uma, ESC no fim"),
                  ("s", "painel de ajustes do driver (Windows)"),
                  ("e  E", "diminuir / aumentar a exposição"),
                  ("q", "salvar em config.json e fechar")]
        for i, (k, t) in enumerate(teclas):
            tk.Label(g, text=k, font=self.f_negrito, bg=COR["cinza"], fg=COR["texto"], padx=8,
                     width=4).grid(row=i, column=0, sticky="nw", pady=3)
            self._quebra(self.label(g, t)).grid(row=i, column=1, sticky="ew", padx=(10, 0), pady=3)
        c, b = self.card(p, "Calibração salva")
        c.pack(fill="x")
        self.l_config = self._quebra(self.label(b, ""))
        self.l_config.pack(fill="x", pady=(4, 0))

    # ---------- pagina 2
    def _pagina_bruto(self):
        p = self.pagina("bruto", "2 · Dados brutos (antes do hash)",
                        "Grava 1.000.000 de amostras (bit menos significativo de cada pixel) em três ordens de "
                        "leitura: horizontal, vertical e temporal. O NIST SP 800-90B mede a min-entropia de "
                        "cada arquivo e o menor valor vira o h.")
        lin = self.linha2(p)
        c, b = self.card(lin, "Na câmera", faixa=COR["azul"], etiqueta="Windows")
        self.em_coluna(c, 0)
        g = tk.Frame(b, bg=COR["card"])
        g.pack(fill="x", pady=(4, 0))
        g.columnconfigure(1, weight=1)
        for i, (chave, nome, desc) in enumerate(FONTES):
            ttk.Radiobutton(g, text=nome, value=chave, variable=self.v_fonte,
                            style="Card.TRadiobutton").grid(row=i, column=0, sticky="w", pady=3)
            self._quebra(self.label(g, desc, self.f_peq, COR["suave"])).grid(row=i, column=1, sticky="ew",
                                                                              padx=(10, 0))
        self.botao(b, "Gravar dados brutos", self.acao_bruto).pack(anchor="w", pady=(10, 0))
        c, b = self.card(lin, "No NIST · SP 800-90B", faixa=COR["roxo"], etiqueta="WSL",
                         nota="Roda o ea_non_iid nos três arquivos da fonte escolhida ao lado "
                              "(trilha não-IID). Pode levar alguns minutos.")
        self.em_coluna(c, 1)
        self.botao(b, "Avaliar no NIST (ea_non_iid)", self.acao_non_iid, "roxo").pack(anchor="w", pady=(4, 0))
        c, b = self.card(p, "Resultados")
        c.pack(fill="x")
        self.l_h_bruto = self._quebra(self.label(b, "", self.f_negrito))
        self.l_h_bruto.pack(fill="x", pady=(4, 8))
        f, self.t_bruto = self.tabela(b, ["Fonte", "Arquivo", "Prévia (só MCV)", "H_original (NIST)"],
                                      [190, 230, 130, 150], altura=6)
        f.pack(fill="x")

    # ---------- pagina 3
    def _pagina_reinicio(self):
        p = self.pagina("reinicio", "3 · Teste de reinício",
                        "Fecha e reabre a câmera 1000 vezes e guarda as 1000 primeiras amostras de cada vez "
                        "(matriz 1000 × 1000). Mostra se a entropia logo depois de ligar a câmera é a mesma "
                        "do funcionamento contínuo.")
        lin = self.linha2(p)
        c, b = self.card(lin, "Na câmera", faixa=COR["azul"], etiqueta="Windows",
                         nota="Se for interrompido, clique de novo: continua de onde parou.")
        self.em_coluna(c, 0)
        self.l_reinicio_arq = self.label(b, "")
        self.l_reinicio_arq.pack(fill="x")
        ttk.Checkbutton(b, text="Recomeçar do zero (apaga os reinícios já gravados)", variable=self.v_do_zero,
                        style="Card.TCheckbutton").pack(anchor="w", pady=(6, 0))
        self.botao(b, "Gravar reinícios", self.acao_reinicio).pack(anchor="w", pady=(10, 0))
        c, b = self.card(lin, "No NIST · SP 800-90B", faixa=COR["roxo"], etiqueta="WSL",
                         nota="Se passar, o h da geração vira min(H_r, H_c, H_I): a própria ferramenta do "
                              "NIST indica esse valor como a entropia validada para o condicionamento.")
        self.em_coluna(c, 1)
        self.l_hi = self.label(b, "")
        self.l_hi.pack(fill="x")
        self.botao(b, "Validar no NIST (ea_restart)", self.acao_restart, "roxo").pack(anchor="w", pady=(10, 0))
        c, b = self.card(p, "Resultado")
        c.pack(fill="x")
        self.l_restart = self._quebra(self.label(b, "", self.f_negrito))
        self.l_restart.pack(fill="x")
        self.l_restart2 = self._quebra(self.label(b, "", self.f_peq, COR["suave"]))
        self.l_restart2.pack(fill="x", pady=(4, 0))

    # ---------- pagina 4
    def _pagina_gerar(self):
        p = self.pagina("gerar", "4 · Gerar a saída (SHA-256)",
                        "Junta amostras com pelo menos 2 × 256 bits de min-entropia e aplica o SHA-256. "
                        "Durante a geração rodam os testes de saúde do SP 800-90B (Repetition Count e "
                        "Adaptive Proportion).")
        lin = self.linha2(p)
        c, b = self.card(lin, "Na câmera", faixa=COR["azul"], etiqueta="Windows")
        self.em_coluna(c, 0)
        g = tk.Frame(b, bg=COR["card"])
        g.pack(fill="x", pady=(4, 0))
        g.columnconfigure(1, weight=1)
        campos = [("h (bits por amostra)", ttk.Entry(g, textvariable=self.v_h, width=12)),
                  ("Margem", ttk.Spinbox(g, from_=1, to=4, increment=0.5, width=6, textvariable=self.v_margem)),
                  ("Sequências de 10⁶ bits", ttk.Spinbox(g, from_=1, to=1000, width=6, textvariable=self.v_seq)),
                  ("Arquivo de saída", ttk.Entry(g, textvariable=self.v_saida, width=22))]
        for i, (rot, w) in enumerate(campos):
            self.label(g, rot).grid(row=2 * i, column=0, sticky="w", pady=3, padx=(0, 12))
            w.grid(row=2 * i, column=1, sticky="w", pady=3)
            if i == 0:
                self.l_h_origem = self.label(g, "", self.f_peq, COR["suave"])
                self.l_h_origem.grid(row=1, column=0, columnspan=2, sticky="w")
        self.l_info_gerar = self._quebra(self.label(b, "", self.f_peq, COR["suave"]))
        self.l_info_gerar.pack(fill="x", pady=(8, 0))
        self.botao(b, "Gerar saída", self.acao_gerar).pack(anchor="w", pady=(10, 0))
        c, b = self.card(lin, "No NIST · SP 800-90B", faixa=COR["roxo"], etiqueta="WSL",
                         nota="Confere na ferramenta do NIST quanta min-entropia sai do SHA-256 "
                              "(componente aprovado, seção 3.1.5).")
        self.em_coluna(c, 1)
        self.l_cmd_cond = self._quebra(tk.Label(b, text="", font=self.f_mono, bg=COR["cinza"], fg=COR["texto"],
                                                anchor="w", justify="left", padx=8, pady=6))
        self.l_cmd_cond.pack(fill="x", pady=(2, 8))
        self.botao(b, "Conferir h_out (ea_conditioning)", self.acao_conditioning, "roxo").pack(anchor="w")
        self.l_cond = self._quebra(self.label(b, "", self.f_negrito))
        self.l_cond.pack(fill="x", pady=(10, 0))
        c, b = self.card(p, "Arquivo gerado")
        c.pack(fill="x")
        self.l_saida = self._quebra(self.label(b, ""))
        self.l_saida.pack(fill="x")

    # ---------- pagina 5
    def _pagina_testar(self):
        p = self.pagina("testar", "5 · Testes estatísticos (SP 800-22)",
                        "Monobit, Frequência em Blocos e Runs em 100 sequências de 1.000.000 de bits, com a "
                        "análise de 2º nível (proporção e uniformidade). Rode no programa e na bateria oficial "
                        "do NIST para comparar os dois.")
        f = tk.Frame(p, bg=COR["fundo"])
        f.pack(fill="x")
        tk.Label(f, text="Arquivo", font=self.f_negrito, bg=COR["fundo"], fg=COR["texto"]).pack(side="left", padx=(0, 8))
        self.cb_arquivo = ttk.Combobox(f, textvariable=self.v_arquivo, width=36)
        self.cb_arquivo.pack(side="left")
        self.botao(f, "Procurar…", self.procurar_arquivo, "cinza", acao=False, pequeno=True).pack(side="left", padx=8)
        ttk.Checkbutton(f, text="Arquivo bruto (1 bit por byte)", variable=self.v_bits,
                        style="Fundo.TCheckbutton").pack(side="left", padx=(8, 0))
        self.l_arq_info = self.label(p, "", self.f_peq, COR["suave"], bg=COR["fundo"])
        self.l_arq_info.pack(fill="x", pady=(4, 10))
        lin = self.linha2(p)
        c, b = self.card(lin, "No programa (trng_lava.py)", faixa=COR["azul"], etiqueta="Windows",
                         nota="O autoteste confere o código com os exemplos do SP 800-22.")
        self.em_coluna(c, 0)
        f = tk.Frame(b, bg=COR["card"])
        f.pack(anchor="w", pady=(2, 0))
        self.botao(f, "Testar", self.acao_testar).pack(side="left")
        self.botao(f, "Autoteste", self.acao_autoteste, "cinza").pack(side="left", padx=8)
        c, b = self.card(lin, "No NIST · sts-2.1.2", faixa=COR["roxo"], etiqueta="WSL",
                         nota="Roda o assess oficial só com Frequency, BlockFrequency e Runs. O relatório "
                              "fica em sts_<arquivo>.txt nesta pasta.")
        self.em_coluna(c, 1)
        self.botao(b, "Rodar bateria oficial (assess)", self.acao_sts, "roxo").pack(anchor="w", pady=(2, 0))
        c, b = self.card(p, "Resultados: programa × NIST",
                         nota="P-valor = uniformidade dos p-valores das sequências (2º nível). Aprovado quando a "
                              "proporção passa do mínimo e o P-valor é ≥ 0.0001, como no relatório do STS.")
        c.pack(fill="x")
        f, self.t_testes = self.tabela(b, ["Teste", "Prop. programa", "P-valor programa",
                                           "Prop. NIST", "P-valor NIST", "Resultado"],
                                       [170, 130, 140, 110, 120, 110], altura=3)
        f.pack(fill="x")
        self.l_comparacao = self._quebra(self.label(b, "", self.f_negrito))
        self.l_comparacao.pack(fill="x", pady=(8, 0))

    # ---------- pagina 6
    def _pagina_wsl(self):
        p = self.pagina("wsl", "WSL e ferramentas do NIST",
                        "Os botões roxos rodam as ferramentas do NIST no Linux pelo WSL. Na primeira vez, "
                        "clique em “Instalar e compilar”: o painel instala os pacotes, baixa o SP 800-90B "
                        "(GitHub do NIST) e o sts-2.1.2 (site do NIST) e compila tudo.")
        lin = self.linha2(p)
        c, b = self.card(lin, "WSL")
        self.em_coluna(c, 0)
        f = tk.Frame(b, bg=COR["card"])
        f.pack(fill="x", pady=(4, 6))
        self.label(f, "Distribuição").pack(side="left")
        self.cb_distro = ttk.Combobox(f, textvariable=self.v_distro, width=18, values=["(padrão)"])
        self.cb_distro.pack(side="left", padx=8)
        self.l_verif = self._quebra(self.label(b, "Verificando…"))
        self.l_verif.pack(fill="x", pady=(2, 8))
        self.botao(b, "Verificar", self.acao_verificar, "cinza").pack(anchor="w")
        c, b = self.card(lin, "Pastas no WSL", nota="O padrão é a pasta pessoal do Linux (~).")
        self.em_coluna(c, 1)
        g = tk.Frame(b, bg=COR["card"])
        g.pack(fill="x")
        g.columnconfigure(0, weight=1)
        for i, (rot, var) in enumerate((("SP 800-90B (pasta cpp)", self.v_90b),
                                        ("sts-2.1.2 (pasta com o assess)", self.v_sts))):
            self.label(g, rot, self.f_peq, COR["suave"]).grid(row=2 * i, column=0, sticky="w", pady=(4, 0))
            ttk.Entry(g, textvariable=var).grid(row=2 * i + 1, column=0, sticky="ew")
        lin = self.linha2(p)
        c, b = self.card(lin, "Instalar e compilar", faixa=COR["roxo"], etiqueta="WSL",
                         nota="Instala os pacotes como root (sem pedir senha), baixa o código do NIST nas "
                              "pastas ao lado e compila. Pode rodar de novo: só faz o que falta.")
        self.em_coluna(c, 0)
        self.botao(b, "Instalar e compilar", self.acao_instalar, "roxo").pack(anchor="w")
        c, b = self.card(lin, "Python no Windows")
        self.em_coluna(c, 1)
        self.l_python = self._quebra(self.label(b, "", self.f_peq, COR["suave"]))
        self.l_python.pack(fill="x")
        self.l_modulos = self._quebra(self.label(b, "Verificando numpy e opencv…"))
        self.l_modulos.pack(fill="x", pady=(4, 8))
        self.botao(b, "Instalar numpy e opencv", self.acao_pip, "cinza").pack(anchor="w")

    # ---------- registro (log)
    def _area_log(self, pai):
        barra = tk.Frame(pai, bg=COR["card"], padx=14, pady=8)
        barra.pack(fill="x")
        self.l_status = tk.Label(barra, textvariable=self.v_status, font=self.f_negrito, bg=COR["card"],
                                 fg=COR["texto"], anchor="w")
        self.l_status.pack(side="left", fill="x", expand=True)
        self.b_limpar = self.botao(barra, "Limpar", self.limpar_log, "cinza", acao=False, pequeno=True)
        self.b_limpar.pack(side="right")
        self.b_parar = self.botao(barra, "Parar", self.parar, "vermelho", acao=False, pequeno=True)
        self.b_parar.pack(side="right", padx=8)
        self.barra = ttk.Progressbar(barra, length=240, mode="determinate", maximum=100,
                                     style="Azul.Horizontal.TProgressbar")
        self.barra.pack(side="right", padx=8)
        self._ligar(self.b_parar, False)
        f = tk.Frame(pai, bg=COR["log_fundo"])
        f.pack(fill="both", expand=True)
        self.log = tk.Text(f, bg=COR["log_fundo"], fg=COR["log_texto"], insertbackground=COR["log_texto"],
                           font=self.f_mono, relief="flat", bd=0, padx=12, pady=8, wrap="none",
                           selectbackground="#334155", state="disabled")
        sb = ttk.Scrollbar(f, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("cab", foreground="#93c5fd", font=(self.f_mono.actual("family"), 9, "bold"))
        self.log.tag_configure("cmd", foreground="#c4b5fd")
        self.log.tag_configure("ok", foreground="#86efac", font=(self.f_mono.actual("family"), 9, "bold"))
        self.log.tag_configure("erro", foreground="#fca5a5", font=(self.f_mono.actual("family"), 9, "bold"))
        self.log.tag_configure("aviso", foreground="#fcd34d")
        self._log_linha("Registro: tudo o que os programas escrevem aparece aqui.", "aviso")

    # ------------------------------------------------------------------ estado
    def bits(self):
        c = ler_config_json() or {}
        try:
            return max(1, min(8, int(c.get("bits_por_amostra", 1))))
        except (TypeError, ValueError):
            return 1

    def distro(self):
        d = self.v_distro.get().strip()
        return "" if d.startswith("(") else d

    def res(self):
        return self.cfg["resultados"]

    def salvar(self):
        c = self.cfg
        c["camera"] = int(para_float(self.v_camera.get()) or 0)
        c["fonte"] = self.v_fonte.get()
        c["h"] = self.v_h.get().strip()
        c["h_origem"] = self.v_h_origem.get()
        c["margem"] = para_float(self.v_margem.get()) or 2.0
        c["sequencias"] = int(para_float(self.v_seq.get()) or 100)
        c["saida"] = self.v_saida.get().strip() or "saida_trng.bin"
        c["arquivo_teste"] = self.v_arquivo.get().strip()
        c["distro"] = self.distro()
        c["dir_90b"] = self.v_90b.get().strip()
        c["dir_sts"] = self.v_sts.get().strip()
        try:
            with open(ARQ_PAINEL, "w", encoding="utf-8") as f:
                json.dump(c, f, indent=2, ensure_ascii=False)
        except OSError:
            pass

    def definir_h(self, valor, origem):
        self._definindo_h = True
        self.v_h.set("" if valor is None else f"{valor:.6f}")
        self._definindo_h = False
        self.v_h_origem.set(origem)
        self.salvar()

    def estado_passos(self):
        r = self.res()
        c = ler_config_json()
        e = {"calibrar": bool(c and c.get("rois"))}
        arqs = [f"bruto_{o}.bin" for o in ORDENS]
        if all(a in r["non_iid"] for a in arqs):
            e["bruto"] = True
        elif all(tamanho(a) for a in arqs):
            e["bruto"] = "meio"
        rs = r.get("restart")
        if rs and rs.get("ok"):
            e["reinicio"] = True
        elif tamanho("bruto_reinicio.bin") >= 1_000_000:
            e["reinicio"] = "meio"
        saida = self.v_saida.get().strip() or "saida_trng.bin"
        alvo = int(para_float(self.v_seq.get()) or 100) * N_BITS // 8
        if tamanho(saida) >= alvo > 0:
            e["gerar"] = True
        elif tamanho(saida):
            e["gerar"] = "meio"
        if r["testes"].get(saida):
            e["testar"] = True
        if self.verif is not None:
            e["wsl"] = True if (self.verif.get("wsl") and not self.verif.get("faltam")) else \
                ("meio" if self.verif.get("wsl") else False)
        return e

    def atualizar_tudo(self):
        e = self.estado_passos()
        for chave, it in self.itens.items():
            v = e.get(chave)
            it["marca"].configure(text="✓" if v is True else ("•" if v == "meio" else ""),
                                  fg=COR["verde"] if v is True else COR["ambar"])
        self.atualizar_calibrar()
        self.atualizar_bruto()
        self.atualizar_reinicio()
        self.atualizar_gerar()
        self.atualizar_testes()
        self.atualizar_wsl()

    def atualizar_calibrar(self):
        c = ler_config_json()
        if not c or not c.get("rois"):
            self.l_config.configure(text="Nenhuma calibração salva ainda. Clique em “Abrir câmera e calibrar” "
                                         "e use a tecla r para marcar as lâmpadas.", fg=COR["suave"])
            return
        p = max(1, int(c.get("passo", 1)))
        total = sum(len(range(0, int(h), p)) * len(range(0, int(w), p)) for _, _, w, h in c["rois"])
        props = c.get("props") or {}
        partes = [f"{len(c['rois'])} lâmpada(s) marcada(s)", f"{fmt(total)} amostras por quadro",
                  f"câmera {c.get('camera', 0)}", f"{c.get('largura')}×{c.get('altura')}"]
        for nome, rot in (("CAP_PROP_EXPOSURE", "exposição"), ("CAP_PROP_GAIN", "ganho")):
            if isinstance(props.get(nome), (int, float)):
                partes.append(f"{rot} {props[nome]:g}")
        if int(c.get("bits_por_amostra", 1)) != 1:
            partes.append(f"{c['bits_por_amostra']} bits por amostra")
        self.l_config.configure(text="  ·  ".join(partes), fg=COR["texto"])

    def atualizar_bruto(self):
        t, r = self.t_bruto, self.res()["non_iid"]
        t.delete(*t.get_children())
        menor = {}
        for chave, nome, _ in FONTES:
            info = ler_json(os.path.join(PASTA, f"{chave}_info.json")) or {}
            previas = info.get("previa_mcv", {})
            for o in ORDENS:
                arq = f"{chave}_{o}.bin"
                if not (tamanho(arq) or arq in r):
                    continue
                h = r.get(arq)
                pv = previas.get(arq)
                t.insert("", "end", iid=arq, values=(nome, arq, "—" if pv is None else f"{pv:.4f}",
                                                    "falta avaliar" if h is None else f"{h:.6f}"),
                         tags=("pendente",) if h is None else ())
                if h is not None:
                    menor.setdefault(chave, []).append((h, arq))
        textos = []
        for chave, lst in menor.items():
            if len(lst) == len(ORDENS):
                h, arq = min(lst)
                t.item(arq, tags=("forte",))
                if chave == "bruto":
                    textos.insert(0, f"h = {h:.6f}  (menor H_original das lâmpadas fluindo, em {arq})")
                else:
                    textos.append(f"{NOME_FONTE[chave]}: menor H = {h:.6f}")
        if not t.get_children():
            self.l_h_bruto.configure(text="Nenhum dado bruto ainda.", fg=COR["suave"])
        elif not textos:
            self.l_h_bruto.configure(text="Avalie os três arquivos no NIST para obter o h.", fg=COR["suave"])
        else:
            self.l_h_bruto.configure(text="\n".join(textos), fg=COR["texto"])

    def atualizar_reinicio(self):
        n = tamanho("bruto_reinicio.bin") // 1000
        if n:
            self.l_reinicio_arq.configure(text=f"bruto_reinicio.bin: {fmt(min(n, 1000))} de 1.000 reinícios gravados",
                                          fg=COR["verde"] if n >= 1000 else COR["ambar"])
        else:
            self.l_reinicio_arq.configure(text="Nenhum reinício gravado ainda.", fg=COR["suave"])
        hi = self.cfg.get("h_nist")
        self.l_hi.configure(text=f"H_I (do passo 2): {hi:.6f}" if hi else "H_I: avalie os dados brutos no passo 2 primeiro.",
                            fg=COR["texto"] if hi else COR["suave"])
        rs = self.res().get("restart")
        if not rs:
            self.l_restart.configure(text="Ainda não validado.", fg=COR["suave"])
            self.l_restart2.configure(text="")
        elif rs.get("ok"):
            self.l_restart.configure(text=f"✓ Aprovado  ·  h validado = min(H_r, H_c, H_I) = {rs['min']:.6f}",
                                     fg=COR["verde"])
            self.l_restart2.configure(text=f"H_r = {rs.get('H_r', 0):.6f}   H_c = {rs.get('H_c', 0):.6f}   "
                                           f"H_I = {rs.get('H_I', 0):.6f}   ({rs.get('data', '')})")
        else:
            self.l_restart.configure(text="✗ Reprovado: " + (rs.get("motivo") or "veja o registro"), fg=COR["erro"])
            self.l_restart2.configure(text="Pela norma, o h deve ser revisto antes de gerar (veja o registro).")

    def atualizar_gerar(self):
        if not hasattr(self, "l_info_gerar"):
            return
        k = self.bits()
        h, m = para_float(self.v_h.get()), para_float(self.v_margem.get())
        self.l_h_origem.configure(text=self.v_h_origem.get())
        if h and m and 0 < h <= k and m > 0:
            n_s, n_in, h_in = calc_condicionamento(h, m, k)
            seq = int(para_float(self.v_seq.get()) or 100)
            self.l_info_gerar.configure(
                text=f"Cada saída de 256 bits = SHA-256 de {fmt(n_s)} amostras (h_in = {h_in:.1f} bits). "
                     f"Total: {seq} × 1.000.000 de bits.")
            self.l_cmd_cond.configure(text=f"./ea_conditioning -v {n_in} 256 256 {h_in:.6f}")
        else:
            self.l_info_gerar.configure(text=f"Informe o h (entre 0 e {k}); ele vem sozinho dos passos 2 e 3.")
            self.l_cmd_cond.configure(text="./ea_conditioning -v <n_in> 256 256 <h_in>")
        cond = self.res().get("conditioning")
        if cond and h and abs(cond.get("h", -1) - h) < 1e-9 and abs(cond.get("margem", -1) - (m or 0)) < 1e-9:
            self.l_cond.configure(text=f"✓ h_out = {cond['h_out']:g} de 256 bits (NIST)", fg=COR["verde"])
        else:
            self.l_cond.configure(text="")
        saida = self.v_saida.get().strip() or "saida_trng.bin"
        tam = tamanho(saida)
        self.l_saida.configure(text=f"{saida}: {fmt(tam)} bytes ({fmt(tam * 8)} bits)" if tam else
                               f"{saida}: ainda não gerado.", fg=COR["texto"] if tam else COR["suave"])

    def atualizar_testes(self):
        t = self.t_testes
        t.delete(*t.get_children())
        arq = self.v_arquivo.get().strip()
        dados = self.res()["testes"].get(arq, {})
        prog = {canonico(l["teste"]): l for l in dados.get("trng", [])}
        nist = {canonico(l["teste"]): l for l in dados.get("sts", [])}
        iguais, ambos = [], 0
        for chave, nome in (("frequency", "Frequency (Monobit)"), ("blockfrequency", "Block Frequency"),
                            ("runs", "Runs")):
            a, b = prog.get(chave), nist.get(chave)
            if not (a or b):
                continue
            ok = all(x["ok"] for x in (a, b) if x)
            t.insert("", "end", values=(nome, a["prop"] if a else "—", a["unif"] if a else "—",
                                        b["prop"] if b else "—", b["unif"] if b else "—",
                                        "aprovado" if ok else "REPROVADO"), tags=("ok",) if ok else ("falha",))
            if a and b:
                ambos += 1
                iguais.append(a["prop"] == b["prop"] and a["unif"] == b["unif"])
        if not t.get_children():
            self.l_comparacao.configure(text="")
        elif ambos == 0:
            self.l_comparacao.configure(text="Rode o programa e a bateria oficial do NIST para comparar os dois.",
                                        fg=COR["suave"])
        elif all(iguais):
            self.l_comparacao.configure(text=f"✓ Programa e NIST deram os mesmos valores nos {ambos} testes.",
                                        fg=COR["verde"])
        else:
            self.l_comparacao.configure(text="Programa e NIST deram valores diferentes: confira o registro.",
                                        fg=COR["ambar"])
        tam = tamanho(arq) if arq else 0
        if not arq:
            self.l_arq_info.configure(text="")
        elif not tam:
            self.l_arq_info.configure(text="Arquivo não encontrado nesta pasta.")
        else:
            bits = tam if self.v_bits.get() else tam * 8
            self.l_arq_info.configure(text=f"{fmt(tam)} bytes = {fmt(bits)} bits = {bits // N_BITS} sequência(s) "
                                           f"de 1.000.000 de bits")

    def atualizar_wsl(self):
        v = self.verif
        if v is None:
            txt, cor, topo = "Verificando…", COR["suave"], "WSL: verificando…"
        elif v.get("erro"):
            txt, cor, topo = v["erro"], COR["erro"], "WSL: não encontrado"
        elif not v.get("wsl"):
            txt, cor, topo = "O WSL não respondeu. Veja o registro.", COR["erro"], "WSL: sem resposta"
        elif v.get("faltam"):
            txt = (f"WSL ok ({v.get('kernel', '')}). Faltam: {', '.join(v['faltam'])}. "
                   f"Clique em “Instalar e compilar”.")
            cor, topo = COR["ambar"], "WSL ✓  ·  NIST: falta instalar"
        else:
            txt = f"WSL ok ({v.get('kernel', '')}). Ferramentas do NIST prontas: {', '.join(v['tem'])}."
            cor, topo = COR["verde"], "WSL ✓  ·  NIST ✓"
        self.l_verif.configure(text=txt, fg=cor)
        self.l_wsl_topo.configure(text=topo, fg=cor)
        self.l_python.configure(text=f"Python: {python_filho()}")
        m = self.modulos
        if m is None:
            self.l_modulos.configure(text="Verificando numpy e opencv…", fg=COR["suave"])
        else:
            faltam = [k for k, ok in m.items() if not ok]
            self.l_modulos.configure(text=("Faltam: " + ", ".join(faltam)) if faltam else "numpy ✓   opencv ✓",
                                     fg=COR["erro"] if faltam else COR["verde"])

    # ------------------------------------------------------------------ comandos
    def cmd_trng(self, *args):
        return [python_filho(), "-u", SCRIPT] + [str(a) for a in args]

    def cmd_bash(self, script, root=False):
        script = "set -f; IFS=; " + self.prefixo_wsl() + script
        if NO_WINDOWS:
            argv = [wsl_exe()]
            if self.distro():
                argv += ["-d", self.distro()]
            if root:
                argv += ["-u", "root"]
            return argv + ["-e", "bash", "-c", script]
        return (["sudo", "-n", "bash", "-c", script] if root and os.geteuid() != 0
                else ["bash", "-c", script])

    def caminho_bash(self, caminho):
        """Caminho do Windows -> expressao do bash que da o caminho no Linux."""
        caminho = os.path.abspath(caminho)
        return f"$(wslpath -a {sq(caminho)})" if NO_WINDOWS else sq(caminho)

    def prefixo_wsl(self):
        return (f"D={self.caminho_bash(PASTA)}; B={dir_bash(self.v_90b.get())}; "
                f"S={dir_bash(self.v_sts.get())}; ")

    def script_verificar(self):
        return ("echo WSL_OK $(uname -sr); echo Pasta do SP 800-90B: $B; echo Pasta do sts-2.1.2: $S; "
                "for x in ea_non_iid ea_restart ea_conditioning; do "
                "if test -x $B/$x; then echo TEM $x; else echo FALTA $x; fi; done; "
                "if test -x $S/assess; then echo TEM assess; else echo FALTA assess; fi")

    def checar_ferramenta(self, pasta, nome):
        return (f"cd {pasta} 2>/dev/null || {{ echo ERRO: pasta {pasta} nao existe no WSL. Use a aba WSL e NIST.; "
                f"exit 3; }}; test -x ./{nome} || {{ echo ERRO: {nome} nao encontrado em {pasta}. "
                f"Use a aba WSL e NIST, botao Instalar e compilar.; exit 3; }}; ")

    # ------------------------------------------------------------------ execucao
    def executar(self, tarefas, ao_fim=None):
        if self.ocupado:
            messagebox.showinfo("Aguarde", "Já tem uma tarefa rodando. Espere terminar ou clique em Parar.")
            return
        self.salvar()
        self.lote, self.lote_total, self.lote_ok, self.lote_fim = list(tarefas), len(tarefas), True, ao_fim
        self.parar_pedido = False
        self._ocupar(True)
        self._proxima()

    def _proxima(self):
        if self.parar_pedido or not self.lote or not self.lote_ok:
            ok = self.lote_ok and not self.parar_pedido and not self.lote
            self._ocupar(False)
            self.tarefa = None
            if self.parar_pedido:
                self._status("Parado.", COR["ambar"])
            elif ok:
                self._status("Concluído ✓", COR["verde"])
                self.barra.configure(mode="determinate", value=100)
            fim, self.lote_fim = self.lote_fim, None
            if fim:
                fim(ok)
            self.atualizar_tudo()
            return
        t = self.lote.pop(0)
        self.tarefa = t
        i = self.lote_total - len(self.lote)
        rotulo = t["titulo"] + (f"  ({i}/{self.lote_total})" if self.lote_total > 1 else "")
        t["i"] = i
        self._status("Rodando: " + rotulo, COR["azul"])
        self._log_linha("")
        self._log_linha(f"▶ {time.strftime('%H:%M:%S')}  {rotulo}", "cab")
        self._log_linha("$ " + t.get("mostrar", " ".join(t["argv"])), "cmd")
        self.barra.stop()
        if t.get("progresso") or self.lote_total > 1:
            self.barra.configure(mode="determinate", value=100 * (i - 1) / self.lote_total)
        else:
            self.barra.configure(mode="indeterminate")
            self.barra.start(12)
        threading.Thread(target=self._rodar, args=(t,), daemon=True).start()

    def _rodar(self, t):
        env = os.environ.copy()
        env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", WSL_UTF8="1")
        try:
            p = subprocess.Popen(t["argv"], cwd=PASTA, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, env=env, creationflags=SEM_JANELA,
                                 start_new_session=not NO_WINDOWS)
        except OSError as e:
            if os.path.basename(t["argv"][0]).lower().startswith("wsl"):
                msg = ("ERRO: o WSL não está instalado. Abra o PowerShell como administrador, rode "
                       "wsl --install, reinicie o computador e abra o painel de novo.")
            else:
                msg = f"ERRO: não consegui iniciar {t['argv'][0]} ({e})"
            self.fila.put(("txt", msg + "\n"))
            self.fila.put(("fim", t, -1, msg))
            return
        self.proc = p
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        partes, utf16 = [], None
        while True:
            bloco = p.stdout.read1(65536)
            if not bloco:
                break
            if utf16 is None:
                utf16 = bloco.count(b"\x00") > len(bloco) // 4
            s = decodificar(bloco) if utf16 else dec.decode(bloco).replace("\x00", "")
            if s:
                partes.append(s)
                self.fila.put(("txt", s))
        rc = p.wait()
        self.proc = None
        self.fila.put(("fim", t, rc, "".join(partes)))

    def _ler_fila(self):
        try:
            while True:
                msg = self.fila.get_nowait()
                if msg[0] == "txt":
                    self._escrever(msg[1])
                elif msg[0] == "fim":
                    self._fim_tarefa(*msg[1:])
                elif msg[0] == "chamar":
                    msg[1]()
        except queue.Empty:
            pass
        self.raiz.after(40, self._ler_fila)

    def _fim_tarefa(self, t, rc, texto):
        if self.cr or self.log.get("end-2c", "end-1c") != "\n":
            self._escrever("\n")
        if self.parar_pedido:
            self._log_linha("■ Parado pelo usuário.", "aviso")
            self.lote_ok = False
        else:
            fim = t.get("fim")
            ok = fim(rc, texto) if fim else (rc == 0)
            if ok:
                self._log_linha("✓ " + t["titulo"] + " — concluído", "ok")
            else:
                self.lote_ok = False
                ultimas = [l.strip() for l in RE_QUEBRA.split(texto) if l.strip() and l not in "\r\n"]
                motivo = (ultimas[-1] if ultimas else f"código de saída {rc}")[:400]
                self._log_linha(f"✗ {t['titulo']} — falhou", "erro")
                self._status("Falhou ✗  —  veja o registro", COR["erro"])
                if ok is not None:
                    messagebox.showerror(t["titulo"], motivo)
        self._proxima()

    def _ocupar(self, sim):
        self.ocupado = sim
        for b in self.botoes:
            self._ligar(b, not sim)
        self._ligar(self.b_parar, sim)
        if not sim:
            self.barra.stop()
            if str(self.barra["mode"]) == "indeterminate":
                self.barra.configure(mode="determinate", value=0)

    def _ligar(self, b, sim):
        b.configure(state="normal" if sim else "disabled", bg=b.cor if sim else COR["desligado"],
                    cursor="hand2" if sim else "arrow")

    def parar(self):
        if not self.ocupado:
            return
        self.parar_pedido = True
        self._status("Parando…", COR["ambar"])
        p = self.proc
        if p and p.poll() is None:
            try:
                if NO_WINDOWS:
                    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                                   creationflags=SEM_JANELA, timeout=15)
                else:
                    os.killpg(p.pid, signal.SIGTERM)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        linux = self.tarefa.get("linux") if self.tarefa else None
        if linux and NO_WINDOWS:
            try:
                subprocess.Popen(self.cmd_bash(f"pkill -x {linux}"), stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=SEM_JANELA)
            except OSError:
                pass

    # ---------- registro
    def _escrever(self, s, tag=None):
        log = self.log
        no_fim = log.yview()[1] >= 0.999
        log.configure(state="normal")
        for parte in RE_QUEBRA.split(s):
            if not parte:
                continue
            if parte in ("\n", "\r\n"):
                log.insert("end", "\n")
                self.cr = False
            elif parte == "\r":
                self.cr = True
            else:
                if self.cr:
                    log.delete("end-1c linestart", "end-1c")
                    self.cr = False
                if tag:
                    log.insert("end", parte, tag)
                else:
                    log.insert("end", parte)
                self._progresso(log.get("end-1c linestart", "end-1c"))
        linhas = int(log.index("end-1c").split(".")[0])
        if linhas > 6000:
            log.delete("1.0", f"{linhas - 5000}.0")
        log.configure(state="disabled")
        if no_fim:
            log.see("end")

    def _log_linha(self, texto, tag=None):
        if self.log.get("end-2c", "end-1c") not in ("\n", "") or self.cr:
            self.cr = False
            self._escrever("\n")
        self._escrever(texto + "\n", tag)

    def _progresso(self, linha):
        t = self.tarefa
        if not t or not t.get("progresso"):
            return
        f = t["progresso"](linha)
        if f is not None:
            i, n = t.get("i", 1), max(1, self.lote_total)
            self.barra.configure(value=100 * ((i - 1) + min(1.0, f)) / n)

    def _status(self, texto, cor=None):
        self.v_status.set(texto)
        self.l_status.configure(fg=cor or COR["texto"])

    def limpar_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.cr = False

    # ------------------------------------------------------------------ acoes: camera
    def acao_calibrar(self):
        cam = int(para_float(self.v_camera.get()) or 0)
        self._log_linha("Dica: a janela da câmera abriu separada; clique nela antes de usar as teclas.", "aviso")
        self.executar([dict(titulo="Calibrar câmera", argv=self.cmd_trng("calibrar", "--camera", cam),
                            mostrar=f"python trng_lava.py calibrar --camera {cam}")])

    def acao_bruto(self):
        pref = self.v_fonte.get()
        if pref == "controle_tampada" and not messagebox.askokcancel(
                "Controle", "Tampe a lente da câmera (sem mexer em mais nada) e clique em OK."):
            return
        if pref == "controle_cera_parada" and not messagebox.askokcancel(
                "Controle", "Deixe as lâmpadas acesas com a cera ainda sólida (sem fluir) e clique em OK."):
            return
        arqs = [f"{pref}_{o}.bin" for o in ORDENS]
        if any(tamanho(a) for a in arqs) and not messagebox.askyesno(
                "Gravar de novo?", f"Já existem dados de “{NOME_FONTE[pref]}”. Gravar de novo substitui os "
                                   f"arquivos e apaga a avaliação do NIST deles. Continuar?"):
            return

        def fim(rc, texto):
            if rc != 0:
                return False
            for a in arqs:
                self.res()["non_iid"].pop(a, None)
            if pref == "bruto":
                self.cfg["h_nist"] = None
                self.res()["restart"] = None
                self.definir_h(None, "dados novos: avalie no NIST (passo 2)")
            self.salvar()
            return rc == 0

        self.executar([dict(titulo=f"Gravar dados brutos ({NOME_FONTE[pref]})",
                            argv=self.cmd_trng("bruto", "--prefixo", pref),
                            mostrar=f"python trng_lava.py bruto --prefixo {pref}",
                            progresso=prog_bruto, fim=fim)])

    def acao_reinicio(self):
        if self.v_do_zero.get() and tamanho("bruto_reinicio.bin") and not messagebox.askyesno(
                "Recomeçar do zero?", "Isso apaga os reinícios já gravados. Continuar?"):
            return
        args = ["reinicio"] + (["--do-zero"] if self.v_do_zero.get() else [])

        def fim(rc, texto):
            self.res()["restart"] = None
            self.salvar()
            return rc == 0

        self.executar([dict(titulo="Gravar reinícios", argv=self.cmd_trng(*args),
                            mostrar="python trng_lava.py " + " ".join(args),
                            progresso=prog_fracao(r"reinicio (\d+)/(\d+)"), fim=fim)])
        self.v_do_zero.set(False)

    def acao_gerar(self):
        k = self.bits()
        h, m = para_float(self.v_h.get()), para_float(self.v_margem.get())
        seq = int(para_float(self.v_seq.get()) or 0)
        saida = self.v_saida.get().strip()
        if not h or not 0 < h <= k:
            messagebox.showwarning("h", f"Informe o h (entre 0 e {k}). Ele vem do passo 2 (ea_non_iid) "
                                        "ou do passo 3 (ea_restart).")
            return
        if not m or m <= 0 or seq <= 0 or not saida:
            messagebox.showwarning("Gerar", "Confira margem, sequências e nome do arquivo de saída.")
            return
        if tamanho(saida) and not messagebox.askyesno("Gerar de novo?", f"{saida} já existe. Substituir?"):
            return
        args = ["gerar", "--h", f"{h:g}", "--margem", f"{m:g}", "--sequencias", seq, "--saida", saida]

        def fim(rc, texto):
            self.res()["testes"].pop(saida, None)
            self.salvar()
            return rc == 0

        self.executar([dict(titulo="Gerar saída (SHA-256)", argv=self.cmd_trng(*args),
                            mostrar="python trng_lava.py " + " ".join(str(a) for a in args),
                            progresso=prog_pct, fim=fim)])

    def acao_testar(self):
        arq = self.v_arquivo.get().strip()
        if not self._arquivo_ok(arq):
            return
        args = ["testar", arq] + (["--entrada", "bits"] if self.v_bits.get() else [])

        def fim(rc, texto):
            linhas = ler_tabela_trng(texto)
            if rc != 0 or not linhas:
                return False
            self.res()["testes"].setdefault(arq, {})["trng"] = linhas
            self.salvar()
            return True

        self.executar([dict(titulo=f"Testar {os.path.basename(arq)}", argv=self.cmd_trng(*args),
                            mostrar="python trng_lava.py " + " ".join(args),
                            progresso=prog_fracao(r"sequencia (\d+)/(\d+)"), fim=fim)])

    def acao_autoteste(self):
        def fim(rc, texto):
            n = len(re.findall(r"\bok\s*$", texto, re.M))
            if rc == 0 and n == 3:
                messagebox.showinfo("Autoteste", "Os 3 testes deram os mesmos p-valores dos exemplos do "
                                                 "SP 800-22 ✓")
                return True
            return False

        self.executar([dict(titulo="Autoteste (exemplos do SP 800-22)", argv=self.cmd_trng("testar", "--autoteste"),
                            mostrar="python trng_lava.py testar --autoteste", fim=fim)])

    # ------------------------------------------------------------------ acoes: NIST no WSL
    def acao_non_iid(self):
        pref, k = self.v_fonte.get(), self.bits()
        arqs = [f"{pref}_{o}.bin" for o in ORDENS]
        faltam = [a for a in arqs if not tamanho(a)]
        if faltam:
            messagebox.showwarning("Dados brutos", f"Grave os dados brutos de “{NOME_FONTE[pref]}” primeiro "
                                                   f"(faltam: {', '.join(faltam)}).")
            return

        def fim_um(arq, rc, texto):
            h = ler_h(texto, k)
            if h is None:
                return False
            self.res()["non_iid"][arq] = h
            self.salvar()
            self.atualizar_bruto()
            return True

        def fim_lote(ok):
            hs = [self.res()["non_iid"].get(a) for a in arqs]
            if not ok or None in hs:
                return
            h, arq = min(zip(hs, arqs))
            self._log_linha(f"Menor H_original ({NOME_FONTE[pref]}): {h:.6f} em {arq}", "ok")
            if pref == "bruto":
                self.cfg["h_nist"] = h
                self.res()["restart"] = None
                self.definir_h(h, "menor H_original do ea_non_iid (passo 2)")
                self._log_linha("Esse é o h: já está preenchido nos passos 3 (H_I) e 4.", "ok")

        tarefas = []
        for a in arqs:
            script = (self.checar_ferramenta("$B", "ea_non_iid") +
                      f"stdbuf -oL -eL ./ea_non_iid -a $D/{a} {k}")
            tarefas.append(dict(titulo=f"NIST ea_non_iid: {a}", argv=self.cmd_bash(script), linux="ea_non_iid",
                                mostrar=f"./ea_non_iid -a {a} {k}", fim=functools.partial(fim_um, a)))
        self.executar(tarefas, fim_lote)

    def acao_restart(self):
        k = self.bits()
        hi = self.cfg.get("h_nist") or para_float(self.v_h.get())
        if not hi:
            messagebox.showwarning("H_I", "Avalie os dados brutos no NIST (passo 2) primeiro: o teste de "
                                          "reinício usa esse h como H_I.")
            return
        n = tamanho("bruto_reinicio.bin")
        if n < 1_000_000:
            messagebox.showwarning("Reinícios", f"bruto_reinicio.bin tem {fmt(n // 1000)} de 1.000 reinícios. "
                                                "Termine de gravar antes de validar.")
            return

        def fim(rc, texto):
            r = ler_restart(texto)
            if "H_I" not in r and "min" not in r and not r["motivo"]:
                return False
            r["data"] = time.strftime("%d/%m/%Y %H:%M")
            self.res()["restart"] = r
            if r["ok"]:
                self.definir_h(r["min"], "validado no teste de reinício: min(H_r, H_c, H_I) (passo 3)")
                self._log_linha(f"Aprovado. h da geração = {r['min']:.6f}", "ok")
            else:
                self._log_linha("Reprovado no teste de reinício: " + (r["motivo"] or ""), "erro")
                messagebox.showwarning("Teste de reinício", "O teste de reinício falhou:\n\n" +
                                       (r["motivo"] or "veja o registro") +
                                       "\n\nA entropia logo depois de ligar a câmera não bate com a medida "
                                       "no passo 2. Revise a calibração/aquecimento antes de gerar.")
            self.salvar()
            return True

        script = self.checar_ferramenta("$B", "ea_restart") + f"stdbuf -oL -eL ./ea_restart -n $D/bruto_reinicio.bin {k} {hi:.6f}"
        self.executar([dict(titulo="NIST ea_restart", argv=self.cmd_bash(script), linux="ea_restart",
                            mostrar=f"./ea_restart -n bruto_reinicio.bin {k} {hi:.6f}", fim=fim)])

    def acao_conditioning(self):
        k = self.bits()
        h, m = para_float(self.v_h.get()), para_float(self.v_margem.get())
        if not h or not 0 < h <= k or not m:
            messagebox.showwarning("h", f"Informe o h (entre 0 e {k}) e a margem.")
            return
        n_s, n_in, h_in = calc_condicionamento(h, m, k)

        def fim(rc, texto):
            v = re.findall(r"h_out\s*=\s*([0-9.]+)", texto)
            if rc != 0 or not v:
                return False
            self.res()["conditioning"] = {"h": h, "margem": m, "n_in": n_in, "h_in": h_in, "h_out": float(v[-1])}
            self.salvar()
            return True

        script = self.checar_ferramenta("$B", "ea_conditioning") + f"stdbuf -oL ./ea_conditioning -v {n_in} 256 256 {h_in:.6f}"
        self.executar([dict(titulo="NIST ea_conditioning", argv=self.cmd_bash(script), linux="ea_conditioning",
                            mostrar=f"./ea_conditioning -v {n_in} 256 256 {h_in:.6f}", fim=fim)])

    def acao_sts(self):
        arq = self.v_arquivo.get().strip()
        if not self._arquivo_ok(arq):
            return
        if self.v_bits.get():
            messagebox.showwarning("Bateria oficial", "A bateria oficial lê 8 bits por byte. Use um arquivo "
                                                      "gerado no passo 4 (não um arquivo bruto).")
            return
        caminho = arq if os.path.isabs(arq) else os.path.join(PASTA, arq)
        m = min(100, os.path.getsize(caminho) * 8 // N_BITS)
        if m < 1:
            messagebox.showwarning("Bateria oficial", "O arquivo tem menos de 1.000.000 de bits.")
            return
        nome = os.path.splitext(os.path.basename(arq))[0]
        respostas = f"0\\ndata/painel_entrada.bin\\n0\\n{SELECAO_STS}\\n0\\n{m}\\n1\\n"
        script = (f"F={self.caminho_bash(caminho)}; " + self.checar_ferramenta("$S", "assess") +
                  f"mkdir -p data; for t in {PASTAS_STS}; do mkdir -p experiments/AlgorithmTesting/$t; done; "
                  "R=experiments/AlgorithmTesting/finalAnalysisReport.txt; rm -f $R; "
                  "ln -sf $F data/painel_entrada.bin || exit 4; "
                  f"printf '{respostas}' | stdbuf -oL ./assess {N_BITS}; "
                  "test -f $R || { echo ERRO: o assess nao gerou o relatorio.; exit 4; }; "
                  "echo; echo ===== finalAnalysisReport.txt =====; cat $R; "
                  f"cp $R $D/sts_{nome}.txt && echo && echo Relatorio copiado para sts_{nome}.txt")

        def fim(rc, texto):
            linhas = ler_tabela_sts(texto)
            if not linhas:
                return False
            self.res()["testes"].setdefault(arq, {})["sts"] = linhas
            self.salvar()
            return True

        self.executar([dict(titulo=f"NIST sts-2.1.2: {os.path.basename(arq)}", argv=self.cmd_bash(script),
                            linux="assess", fim=fim,
                            mostrar=f"./assess {N_BITS}   (arquivo {os.path.basename(arq)}, {m} sequências, "
                                    f"testes Frequency, BlockFrequency e Runs)")])

    def acao_verificar(self):
        def fim(rc, texto):
            self.verif = ler_verificacao(texto)
            if not self.verif["wsl"] and texto.startswith("ERRO: o WSL"):
                self.verif["erro"] = "O WSL não está instalado (no PowerShell como administrador: wsl --install)."
            self.atualizar_tudo()
            return self.verif["wsl"]

        self.executar([dict(titulo="Verificar WSL e ferramentas do NIST", argv=self.cmd_bash(self.script_verificar()),
                            mostrar="(verifica o WSL e procura ea_non_iid, ea_restart, ea_conditioning e assess)",
                            fim=fim)])

    def acao_instalar(self):
        if not messagebox.askokcancel(
                "Instalar e compilar no WSL",
                "O painel vai, dentro do WSL:\n\n"
                "1. instalar os pacotes de compilação (apt, como root)\n"
                "2. baixar o SP 800-90B do GitHub do NIST e compilar ea_non_iid, ea_restart e ea_conditioning\n"
                "3. baixar o sts-2.1.2 do site do NIST e compilar o assess\n\n"
                f"Pastas:\n{self.v_90b.get()}\n{self.v_sts.get()}\n\n"
                "Precisa de internet e leva alguns minutos. Continuar?"):
            return
        apt = f"export DEBIAN_FRONTEND=noninteractive; apt-get update && apt-get install -y {PACOTES_APT}"
        s90 = ("set -e; if test ! -f $B/Makefile; then "
               "case $B in */cpp) ;; *) echo ERRO: a pasta do SP 800-90B deve terminar em /cpp; exit 3;; esac; "
               "R=$(dirname $B); if test -d $R/.git; then echo Repositorio ja existe em $R; "
               f"else mkdir -p $(dirname $R); git clone --depth 1 {URL_90B} $R; fi; fi; "
               "cd $B; make -j$(nproc) non_iid restart conditioning; ls -l ea_non_iid ea_restart ea_conditioning")
        sts = ("set -e; if test ! -f $S/src/assess.c; then T=$(mktemp -d); "
               "echo Baixando o sts-2.1.2 do site do NIST...; "
               f"wget -q -O $T/sts.zip {URL_STS} || curl -fsSL -o $T/sts.zip {URL_STS}; "
               "unzip -q $T/sts.zip -d $T/x; A=$(find $T/x -path '*/src/assess.c' | head -n 1); "
               "if test -z $A; then echo ERRO: assess.c nao encontrado no zip; exit 4; fi; "
               "M=$(dirname $(dirname $A)); mkdir -p $S; cp -r $M/. $S/; rm -rf $T; fi; "
               f"cd $S; mkdir -p obj data; for t in {PASTAS_STS}; do mkdir -p experiments/AlgorithmTesting/$t; done; "
               "make; ls -l assess")
        tarefas = [dict(titulo="Instalar pacotes (apt)", argv=self.cmd_bash(apt, root=True), linux="apt-get",
                        mostrar=f"sudo apt-get install -y {PACOTES_APT}"),
                   dict(titulo="Baixar e compilar o SP 800-90B", argv=self.cmd_bash(s90), linux="make",
                        mostrar=f"git clone {URL_90B} && make non_iid restart conditioning"),
                   dict(titulo="Baixar e compilar o sts-2.1.2", argv=self.cmd_bash(sts), linux="make",
                        mostrar=f"wget {URL_STS} && make"),
                   dict(titulo="Verificar", argv=self.cmd_bash(self.script_verificar()),
                        mostrar="(verifica as ferramentas)", fim=self._fim_verificacao)]
        self.executar(tarefas)

    def _fim_verificacao(self, rc, texto):
        self.verif = ler_verificacao(texto)
        self.atualizar_tudo()
        return self.verif["wsl"] and not self.verif["faltam"]

    def acao_pip(self):
        def fim(rc, texto):
            self._checar_python()
            return rc == 0

        self.executar([dict(titulo="Instalar numpy e opencv-python",
                            argv=[python_filho(), "-m", "pip", "install", "--upgrade", "numpy", "opencv-python"],
                            mostrar="python -m pip install --upgrade numpy opencv-python", fim=fim)])

    # ------------------------------------------------------------------ checagens em segundo plano
    def checagens_iniciais(self):
        argv = self.cmd_bash(self.script_verificar())
        threading.Thread(target=self._checar_python, daemon=True).start()
        threading.Thread(target=self._checar_wsl, args=(argv,), daemon=True).start()
        if NO_WINDOWS:
            threading.Thread(target=self._listar_distros, daemon=True).start()

    def _rodar_silencioso(self, argv, timeout=120):
        r = subprocess.run(argv, cwd=PASTA, capture_output=True, timeout=timeout, creationflags=SEM_JANELA,
                           env=dict(os.environ, WSL_UTF8="1"), stdin=subprocess.DEVNULL)
        return r.returncode, decodificar(r.stdout + r.stderr)

    def _checar_python(self):
        codigo = ("import importlib\nfor m in ('numpy', 'cv2'):\n    try:\n        importlib.import_module(m)\n"
                  "        print(m, 'ok')\n    except Exception:\n        print(m, 'falta')\n")
        try:
            _, out = self._rodar_silencioso([python_filho(), "-c", codigo], 120)
            mods = {"numpy": "numpy ok" in out, "opencv": "cv2 ok" in out}
        except Exception:
            mods = {"numpy": False, "opencv": False}

        def aplicar():
            self.modulos = mods
            self.atualizar_wsl()
        self.fila.put(("chamar", aplicar))

    def _checar_wsl(self, argv):
        try:
            _, out = self._rodar_silencioso(argv, 180)
            v = ler_verificacao(out)
            if not v["wsl"]:
                v["erro"] = "O WSL não respondeu: " + (out.strip().splitlines() or ["sem mensagem"])[-1][:200]
        except FileNotFoundError:
            v = {"wsl": False, "erro": "O WSL não está instalado (no PowerShell como administrador: wsl --install)."}
        except Exception as e:
            v = {"wsl": False, "erro": f"Não consegui verificar o WSL: {e}"}

        def aplicar():
            self.verif = v
            self.atualizar_tudo()
        self.fila.put(("chamar", aplicar))

    def _listar_distros(self):
        try:
            _, out = self._rodar_silencioso([wsl_exe(), "-l", "-q"], 60)
            nomes = [l.strip() for l in out.splitlines() if l.strip() and not l.strip().startswith("docker-desktop")]
        except Exception:
            nomes = []
        self.fila.put(("chamar", lambda: self.cb_distro.configure(values=["(padrão)"] + nomes)))

    # ------------------------------------------------------------------ arquivos
    def atualizar_lista_arquivos(self):
        nomes = sorted((os.path.basename(p) for p in glob.glob(os.path.join(PASTA, "*.bin"))),
                       key=lambda n: (not n.startswith("saida"), n.startswith(("bruto", "controle")), n))
        self.cb_arquivo.configure(values=nomes)

    def procurar_arquivo(self):
        p = filedialog.askopenfilename(initialdir=PASTA, title="Arquivo para testar",
                                       filetypes=[("Arquivos .bin", "*.bin"), ("Todos", "*.*")])
        if p:
            self.v_arquivo.set(os.path.relpath(p, PASTA) if os.path.dirname(os.path.abspath(p)) == PASTA else p)

    def _auto_bits(self):
        n = os.path.basename(self.v_arquivo.get().strip())
        self.v_bits.set(n.startswith(("bruto_", "controle_")))
        if hasattr(self, "t_testes"):
            self.atualizar_testes()
            self.salvar()

    def _arquivo_ok(self, arq):
        if not arq:
            messagebox.showwarning("Arquivo", "Escolha o arquivo para testar.")
            return False
        caminho = arq if os.path.isabs(arq) else os.path.join(PASTA, arq)
        if not os.path.exists(caminho):
            messagebox.showwarning("Arquivo", f"{arq} não foi encontrado.")
            return False
        return True

    def abrir_pasta(self):
        try:
            if NO_WINDOWS:
                os.startfile(PASTA)
            else:
                subprocess.Popen(["xdg-open", PASTA])
        except Exception:
            pass

    def fechar(self):
        if self.ocupado:
            if not messagebox.askyesno("Fechar", "Uma tarefa está rodando. Parar e fechar o painel?"):
                return
            self.parar()
        self.salvar()
        self.raiz.destroy()


def main():
    if NO_WINDOWS:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    raiz = tk.Tk()
    raiz.title("TRNG · Lâmpadas de lava")
    sw, sh = raiz.winfo_screenwidth(), raiz.winfo_screenheight()
    w, h = min(1200, sw - 40), min(860, sh - 90)
    raiz.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
    raiz.minsize(min(940, sw - 40), min(600, sh - 90))

    def erro_tk(tipo, valor, tb):
        import traceback
        txt = "".join(traceback.format_exception(tipo, valor, tb))
        try:
            with open(os.path.join(PASTA, "painel_erro.txt"), "w", encoding="utf-8") as f:
                f.write(txt)
        except OSError:
            pass
        messagebox.showerror("Erro no painel", txt[-1500:])
    raiz.report_callback_exception = erro_tk
    App(raiz)
    raiz.mainloop()


if __name__ == "__main__":
    main()
