#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trng_lava.py - TRNG com lampadas de lava (TCC)

Camera -> dados brutos para o NIST SP 800-90B -> condicionamento SHA-256 ->
arquivo para a bateria NIST SP 800-22. Inclui os tres testes do recorte do TCC
(Monobit, Frequencia em Blocos e Runs).

Instalacao:  pip install numpy opencv-python
Ajuda:       python trng_lava.py <comando> -h

ORDEM DE USO
 1) calibrar  Mostra a camera ao vivo. Tecla r: marque as 4 lampadas (um retangulo
              por lampada, ENTER depois de cada um, ESC no fim). Ajuste a exposicao
              (tecla s abre o painel do driver no Windows; e/E diminui/aumenta) ate
              "sat" e "esc" ficarem em 0% e "LSB=ant" perto de 0.50. Tecla q salva
              tudo em config.json e sai.

 2) bruto     Grava os dados BRUTOS (antes do hash), 1.000.000 de amostras por
              arquivo. Amostra = bit menos significativo de um pixel em cinza.
                bruto_horizontal.bin   pixels lidos linha a linha
                bruto_vertical.bin     pixels lidos coluna a coluna
                bruto_temporal.bin     1000 pixels fixos ao longo de 1000 quadros
              Cada ordem expoe uma correlacao (vizinho do lado, vizinho de cima,
              mesmo pixel no quadro seguinte). Avalie os tres (trilha nao-IID):
                ./ea_non_iid -a bruto_horizontal.bin 1
              e use como h o MENOR dos tres valores.

 3) reinicio  Fecha e reabre a camera 1000 vezes, guardando as 1000 primeiras
              amostras de cada vez (bruto_reinicio.bin, matriz 1000 x 1000).
                ./ea_restart bruto_reinicio.bin 1 <h>
              Se for interrompido, rode de novo: continua de onde parou.

 4) gerar     Condicionamento SHA-256 com o h medido:
                python trng_lava.py gerar --h 0.65
              Cada saida de 256 bits e o hash de amostras com pelo menos 512 bits
              de min-entropia (margem 2x). Durante a geracao rodam os testes de
              saude do SP 800-90B (secao 4.4): Repetition Count e Adaptive Proportion.

 5) testar    Monobit, Frequencia em Blocos e Runs em 100 sequencias de 10^6 bits,
              com a analise de 2o nivel (proporcao e uniformidade) igual a do
              relatorio do NIST STS:   python trng_lava.py testar saida_trng.bin
              --autoteste confere a implementacao com os exemplos do SP 800-22.

CONTROLES (mostram de onde vem a entropia; rode 'bruto' com outro prefixo)
   python trng_lava.py bruto --prefixo controle_tampada      (lente tampada)
   python trng_lava.py bruto --prefixo controle_cera_parada  (lampadas acesas,
                                                              cera ainda solida)
   e compare o h desses arquivos com o das lampadas fluindo.
"""

import argparse
import hashlib
import json
import math
import os
import sys
import time

import numpy as np

try:  # evita erro de acentuacao quando a saida e redirecionada no Windows
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

cv2 = None  # o OpenCV so e carregado nos comandos que usam a camera


def carregar_cv2():
    global cv2
    if cv2 is None:
        try:
            import cv2 as _cv2
        except ImportError:
            sys.exit("OpenCV nao encontrado. Instale com:  pip install opencv-python")
        cv2 = _cv2
    return cv2


def fmt(n):
    """1234567 -> '1.234.567'"""
    return f"{int(n):,}".replace(",", ".")


# =============================================================================
# Configuracao
# =============================================================================

CONFIG_PADRAO = {
    "camera": 0,              # indice da camera: 0, 1, 2...
    "backend": "auto",        # auto | dshow | msmf | v4l2 | avfoundation
    "largura": 640,
    "altura": 480,
    "fourcc": "",             # "" = YUY2 (Windows) / YUYV (Linux): video SEM compressao
    "aquecimento": 30,        # quadros descartados logo depois de abrir a camera
    "passo": 1,               # usa 1 de cada 'passo' pixels (linhas e colunas) nas ROIs
    "bits_por_amostra": 1,    # bits menos significativos de cada pixel (1 = so o LSB)
    "rois": [],               # [x, y, largura, altura] de cada lampada
    "props": {},              # ajustes extras do OpenCV, aplicados nesta ordem
}


def carregar_config(caminho, exigir_rois=True):
    cfg = json.loads(json.dumps(CONFIG_PADRAO))
    if os.path.exists(caminho):
        with open(caminho, encoding="utf-8") as f:
            cfg.update(json.load(f))
    elif exigir_rois:
        sys.exit(f"{caminho} nao encontrado. Rode primeiro:  python trng_lava.py calibrar")
    if exigir_rois and not cfg["rois"]:
        sys.exit(f"Nenhuma lampada marcada em {caminho}. Rode 'calibrar' e use a tecla r.")
    return cfg


def salvar_config(cfg, caminho):
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def campos_fonte(cfg):
    """Parametros que definem a fonte de ruido: se mudarem, a entropia medida nao vale mais."""
    return {k: cfg.get(k) for k in ("passo", "bits_por_amostra", "rois", "largura", "altura", "fourcc", "props")}


# =============================================================================
# Camera e amostragem
# =============================================================================

def fourcc_int(codigo):
    codigo = (codigo + "    ")[:4]
    return sum(ord(c) << (8 * i) for i, c in enumerate(codigo))


def fourcc_texto(valor):
    v = int(valor)
    s = "".join(chr((v >> (8 * i)) & 0xFF) for i in range(4))
    return s if v > 0 and s.isprintable() else "?"


def id_backend(nome):
    cv = carregar_cv2()
    nome = (nome or "auto").lower()
    if nome == "auto":
        return cv.CAP_DSHOW if os.name == "nt" else cv.CAP_ANY
    tabela = {"dshow": "CAP_DSHOW", "msmf": "CAP_MSMF", "v4l2": "CAP_V4L2",
              "avfoundation": "CAP_AVFOUNDATION", "any": "CAP_ANY"}
    return getattr(cv, tabela.get(nome, "CAP_ANY"), cv.CAP_ANY)


def abrir_camera(cfg, avisos=True):
    cv = carregar_cv2()
    cap = cv.VideoCapture(int(cfg["camera"]), id_backend(cfg.get("backend")))
    if not cap.isOpened():
        sys.exit(f"Nao consegui abrir a camera {cfg['camera']}. Confira o indice "
                 f"(--camera 1, 2...) e feche outros programas que usem a camera.")
    fcc = cfg.get("fourcc") or ("YUY2" if os.name == "nt" else "YUYV")
    if fcc.lower() != "nenhum":
        cap.set(cv.CAP_PROP_FOURCC, fourcc_int(fcc))
    if cfg.get("largura"):
        cap.set(cv.CAP_PROP_FRAME_WIDTH, int(cfg["largura"]))
    if cfg.get("altura"):
        cap.set(cv.CAP_PROP_FRAME_HEIGHT, int(cfg["altura"]))
    for nome, valor in (cfg.get("props") or {}).items():
        prop = getattr(cv, nome, None)
        if prop is None:
            if avisos:
                print(f"  aviso: propriedade desconhecida no config: {nome}")
        elif not cap.set(prop, float(valor)) and avisos:
            print(f"  aviso: a camera recusou {nome} = {valor}")
    for _ in range(int(cfg.get("aquecimento", 30))):
        cap.read()
    return cap


def ler_quadro(cap):
    ok, quadro = cap.read()
    if not ok or quadro is None:
        sys.exit("Falha ao ler um quadro da camera (desconectada ou em uso por outro programa?).")
    return quadro


def para_cinza(quadro):
    cv = carregar_cv2()
    if quadro.ndim == 2:
        return quadro
    if quadro.shape[2] == 4:
        return cv.cvtColor(quadro, cv.COLOR_BGRA2GRAY)
    return cv.cvtColor(quadro, cv.COLOR_BGR2GRAY)


def descrever_camera(cap):
    cv = carregar_cv2()
    fcc = fourcc_texto(cap.get(cv.CAP_PROP_FOURCC))
    w = int(cap.get(cv.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv.CAP_PROP_FRAME_HEIGHT))
    print(f"Camera: {w}x{h}, formato {fcc}, {cap.get(cv.CAP_PROP_FPS):.0f} fps (informado), "
          f"exposicao {cap.get(cv.CAP_PROP_EXPOSURE):g}")
    if fcc.upper().startswith(("MJP", "H26", "AVC", "HEV")):
        print("  ATENCAO: formato comprimido. A compressao apaga e correlaciona o ruido dos bits\n"
              "  menos significativos. Tente outra resolucao (ex.: 640x480) para obter YUY2/YUYV.")
    return fcc, w, h


class Amostrador:
    """Quadro -> cinza (8 bits) -> pixels das ROIs -> 'bits_por_amostra' menos significativos."""

    def __init__(self, cfg):
        self.rois = [tuple(int(v) for v in r) for r in cfg["rois"]]
        self.passo = max(1, int(cfg.get("passo", 1)))
        self.bits = int(cfg.get("bits_por_amostra", 1))
        if not 1 <= self.bits <= 8:
            sys.exit("'bits_por_amostra' deve estar entre 1 e 8.")
        self.mascara = np.uint8((1 << self.bits) - 1)
        self.conferido = False

    def pixels(self, quadro, ordem="h"):
        cinza = para_cinza(quadro)
        if not self.conferido:
            H, W = cinza.shape[:2]
            for x, y, w, h in self.rois:
                if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > W or y + h > H:
                    sys.exit(f"ROI {[x, y, w, h]} esta fora do quadro {W}x{H}. Rode 'calibrar' de novo.")
            self.conferido = True
        p = self.passo
        partes = []
        for x, y, w, h in self.rois:
            r = cinza[y:y + h:p, x:x + w:p]
            partes.append((r.T if ordem == "v" else r).ravel())
        return np.concatenate(partes)

    def amostras(self, pixels):
        return pixels & self.mascara


def previa_mcv(s):
    """SP 800-90B 6.3.1 (valor mais comum). So uma previa: o resultado oficial e o
    MENOR dos estimadores do ea_non_iid, que nunca passa deste valor."""
    L = len(s)
    p = np.bincount(s).max() / L
    pu = min(1.0, p + 2.576 * math.sqrt(p * (1 - p) / (L - 1)))
    return -math.log2(pu)


# =============================================================================
# 1) calibrar
# =============================================================================

def cmd_calibrar(a):
    cv = carregar_cv2()
    cfg = carregar_config(a.config, exigir_rois=False)
    for campo in ("camera", "backend", "largura", "altura", "passo", "bits_por_amostra"):
        v = getattr(a, campo, None)
        if v is not None:
            cfg[campo] = v
    cap = abrir_camera(cfg)
    fcc, W, H = descrever_camera(cap)
    rois = [list(map(int, r)) for r in cfg["rois"]]
    print("Teclas: r = marcar lampadas | s = painel do driver (Windows) | "
          "e/E = exposicao -/+ | q = salvar e sair")
    janela = "TRNG calibrar  (r: lampadas  s: driver  e/E: exposicao  q: salvar)"
    cv.namedWindow(janela, cv.WINDOW_NORMAL)
    ant, repetidos, n, fps, t0 = None, 0, 0, 0.0, time.time()
    while True:
        quadro = ler_quadro(cap)
        cinza = para_cinza(quadro)
        if ant is not None and np.array_equal(cinza, ant):
            repetidos += 1
        vis = cv.cvtColor(cinza, cv.COLOR_GRAY2BGR) if quadro.ndim == 2 else quadro[:, :, :3].copy()
        linhas = []
        for i, (x, y, w, h) in enumerate(rois):
            r = cinza[y:y + h, x:x + w]
            if r.size == 0:
                continue
            sat = 100.0 * np.count_nonzero(r == 255) / r.size
            esc = 100.0 * np.count_nonzero(r == 0) / r.size
            txt = f"L{i + 1}  sat {sat:4.1f}%  esc {esc:4.1f}%"
            if ant is not None and ant.shape == cinza.shape:
                ra = ant[y:y + h, x:x + w]
                d = r.astype(np.int16) - ra.astype(np.int16)
                ruido = 1.4826 * float(np.median(np.abs(d - np.median(d)))) / math.sqrt(2)
                igual = float(np.mean((r & 1) == (ra & 1)))
                txt += f"  ruido {ruido:4.1f}  LSB=ant {igual:.2f}"
            cor = (0, 0, 255) if (sat > 0 or esc > 0) else (0, 220, 0)
            cv.rectangle(vis, (x, y), (x + w, y + h), cor, 2)
            cv.putText(vis, f"L{i + 1}", (x + 3, max(14, y - 6)), cv.FONT_HERSHEY_SIMPLEX, 0.5, cor, 1, cv.LINE_AA)
            linhas.append((txt, cor))
        if linhas:  # painel com os numeros de cada lampada no canto superior esquerdo
            larg = max(cv.getTextSize(t, cv.FONT_HERSHEY_SIMPLEX, 0.45, 1)[0][0] for t, _ in linhas)
            cv.rectangle(vis, (0, 0), (larg + 12, 10 + 18 * len(linhas)), (0, 0, 0), -1)
            for i, (txt, cor) in enumerate(linhas):
                cv.putText(vis, txt, (6, 20 + 18 * i), cv.FONT_HERSHEY_SIMPLEX, 0.45, cor, 1, cv.LINE_AA)
        n += 1
        if n % 15 == 0:
            fps, t0 = 15 / max(1e-9, time.time() - t0), time.time()
        rodape = (f"{W}x{H} {fcc} {fps:.1f} fps | repetidos {repetidos} | "
                  f"exposicao {cap.get(cv.CAP_PROP_EXPOSURE):g} | lampadas {len(rois)}")
        cv.putText(vis, rodape, (8, vis.shape[0] - 10), cv.FONT_HERSHEY_SIMPLEX, 0.45,
                   (255, 255, 0), 1, cv.LINE_AA)
        cv.imshow(janela, vis)
        ant = cinza
        k = cv.waitKey(1) & 0xFF
        if k == ord("r"):
            titulo = "Marque cada lampada: ENTER depois de cada uma, ESC no fim"
            sel = cv.selectROIs(titulo, quadro, showCrosshair=False)
            cv.destroyWindow(titulo)
            rois = [list(map(int, r)) for r in np.asarray(sel).reshape(-1, 4) if r[2] > 0 and r[3] > 0]
            ant = None
        elif k == ord("s"):
            cap.set(cv.CAP_PROP_SETTINGS, 1)
        elif k in (ord("e"), ord("E")):
            atual = cap.get(cv.CAP_PROP_EXPOSURE)
            delta = 1.0 if abs(atual) < 20 else max(1.0, round(abs(atual) * 0.2))
            cap.set(cv.CAP_PROP_EXPOSURE, atual + (delta if k == ord("E") else -delta))
            cfg.setdefault("props", {})["CAP_PROP_EXPOSURE"] = cap.get(cv.CAP_PROP_EXPOSURE)
        elif k in (ord("q"), 27):
            break
    # grava exposicao e ganho atuais: todo comando reabre a camera com eles (modo manual),
    # para o driver nao voltar ao automatico entre a medicao e a geracao
    props = cfg.setdefault("props", {})
    for nome in ("CAP_PROP_EXPOSURE", "CAP_PROP_GAIN"):
        props[nome] = cap.get(getattr(cv, nome))
    print(f"Exposicao {props['CAP_PROP_EXPOSURE']:g} e ganho {props['CAP_PROP_GAIN']:g} gravados "
          f"no config: serao aplicados em todos os comandos.")
    cap.release()
    cv.destroyAllWindows()
    cfg["rois"] = rois
    salvar_config(cfg, a.config)
    if not rois:
        print(f"Salvo em {a.config}, mas sem lampadas marcadas. Rode de novo e use a tecla r.")
        return
    p = max(1, int(cfg["passo"]))
    total = sum(len(range(0, h, p)) * len(range(0, w, p)) for _, _, w, h in rois)
    print(f"Salvo em {a.config}: {len(rois)} lampada(s), {fmt(total)} amostras por quadro "
          f"(~{math.ceil(1_000_000 / total)} quadros por 1.000.000 de amostras).")


# =============================================================================
# 2) bruto - dados para o SP 800-90B (antes de qualquer hash)
# =============================================================================

def cmd_bruto(a):
    cfg = carregar_config(a.config)
    amo = Amostrador(cfg)
    cap = abrir_camera(cfg)
    fcc, _, _ = descrever_camera(cap)
    N, T, P = a.amostras, a.quadros, a.posicoes
    print(f"Gravando dados brutos ({amo.bits} bit(s) por amostra, passo {amo.passo})...")
    hor, ver = [], []
    temporal = np.empty((T, P), dtype=np.uint8)
    n = t = usados = repetidos = 0
    pos, ant = None, None
    while n < N or t < T:
        quadro = ler_quadro(cap)
        pix = amo.pixels(quadro, "h")
        if ant is not None and np.array_equal(pix, ant):
            repetidos += 1          # quadro repetido pelo driver: nao traz amostra nova
            continue
        ant = pix
        usados += 1
        s = amo.amostras(pix)
        if pos is None:
            if len(s) < P:
                sys.exit(f"As lampadas marcadas dao so {len(s)} amostras por quadro; "
                         f"marque areas maiores ou use --posicoes menor.")
            pos = (np.arange(P) * (len(s) / P)).astype(np.int64)   # P pixels espalhados
        if n < N:
            hor.append(s)
            ver.append(amo.amostras(amo.pixels(quadro, "v")))
            n += len(s)
        if t < T:
            temporal[t] = s[pos]
            t += 1
        print(f"\r  quadros {usados}  repetidos {repetidos}  amostras {fmt(min(n, N))}/{fmt(N)}"
              f"  temporal {t}/{T}", end="", flush=True)
    cap.release()
    print()

    arquivos = {
        f"{a.prefixo}_horizontal.bin": np.concatenate(hor)[:N],
        f"{a.prefixo}_vertical.bin": np.concatenate(ver)[:N],
        f"{a.prefixo}_temporal.bin": temporal.T.reshape(-1),   # cada pixel ao longo do tempo
    }
    previas = {}
    print(f"\nArquivos gravados (1 byte = 1 amostra de {amo.bits} bit(s)):")
    for nome, dados in arquivos.items():
        dados.astype(np.uint8).tofile(nome)
        previas[nome] = previa_mcv(dados)
        print(f"  {nome:<28} {fmt(len(dados)):>10} amostras   previa (so MCV): "
              f"{previas[nome]:.3f} bit/amostra")
    info = {"fonte": campos_fonte(cfg), "formato_camera": fcc, "quadros_usados": usados,
            "quadros_repetidos": repetidos, "previa_mcv": previas,
            "data": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(f"{a.prefixo}_info.json", "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)
    print(f"Quadros usados: {usados} (repetidos descartados: {repetidos})")
    print("\nProximo passo, na pasta SP800-90B_EntropyAssessment/cpp (Linux ou WSL):")
    for nome in arquivos:
        print(f"  ./ea_non_iid -a {nome} {amo.bits}")
    linha = "H_original" if amo.bits == 1 else f"min(H_original, {amo.bits} X H_bitstring)"
    print(f"Use como h o MENOR valor da linha '{linha}' entre os tres arquivos.")
    print("A previa MCV so enxerga vies; o resultado oficial costuma ser bem menor.")


# =============================================================================
# 3) reinicio - dados de reinicio do SP 800-90B (secao 3.1.4)
# =============================================================================

def cmd_reinicio(a):
    cfg = carregar_config(a.config)
    amo = Amostrador(cfg)
    R, S, caminho = a.reinicios, a.amostras, a.saida
    feitos = 0
    if os.path.exists(caminho):
        if a.do_zero:
            os.remove(caminho)
        else:
            tam = os.path.getsize(caminho)
            feitos = tam // S
            if tam % S:  # interrompido no meio de uma linha
                with open(caminho, "r+b") as f:
                    f.truncate(feitos * S)
            if feitos:
                print(f"Continuando: {feitos} reinicios ja gravados em {caminho}.")
    if feitos >= R:
        print(f"{caminho} ja tem {R} reinicios.")
    else:
        print(f"Reiniciando a camera {R - feitos} vezes ({S} amostras por reinicio)...")
        t0 = time.time()
        with open(caminho, "ab") as f:
            for r in range(feitos, R):
                cap = abrir_camera(cfg, avisos=(r == feitos))
                quadro = ler_quadro(cap)
                cap.release()
                s = amo.amostras(amo.pixels(quadro, "h"))
                if len(s) < S:
                    sys.exit(f"Cada quadro da so {len(s)} amostras; e preciso pelo menos {S}.")
                f.write(s[:S].tobytes())
                f.flush()
                feito_agora = r - feitos + 1
                resta = (time.time() - t0) / feito_agora * (R - r - 1)
                print(f"\r  reinicio {r + 1}/{R}   ~{resta / 60:.0f} min restantes ", end="", flush=True)
                if a.pausa > 0:
                    time.sleep(a.pausa)
        print()
    print(f"\nProximo passo:  ./ea_restart {caminho} {amo.bits} <h>")
    print("(h = menor min-entropia do passo 'bruto'. O teste precisa passar na 'sanity check'.)")


# =============================================================================
# 4) gerar - condicionamento SHA-256 + testes de saude
# =============================================================================

ALFA_LOG2 = 40  # alarme falso de 2^-40 por amostra nos testes de saude (SP 800-90B 4.4)


def entropia_saida_vetada(n_in, n_out, nw, h_in):
    """SP 800-90B 3.1.5.1.2: min-entropia na saida de um condicionador aprovado (SHA-256).
    Reescrita para nao estourar o float com 2^n_in grande."""
    n = min(n_out, nw)
    p_high = 2.0 ** -h_in
    fator = 1.0 / (1.0 - 2.0 ** -n_in)                    # 2^n_in / (2^n_in - 1)
    psi = (1.0 - p_high) * fator * 2.0 ** -n + p_high
    omega = (1.0 - p_high) * fator * (2.0 ** -n + math.sqrt(2 * n * math.log(2)) * 2.0 ** (-(n_in + n) / 2))
    return -math.log2(max(psi, omega))


def cauda_binomial(W, p, c):
    """P(B >= c), B ~ Binomial(W, p), calculada em escala logaritmica."""
    if c <= 0:
        return 1.0
    if c > W:
        return 0.0
    lp, lq = math.log(p), math.log1p(-p)
    termos = [math.lgamma(W + 1) - math.lgamma(j + 1) - math.lgamma(W - j + 1) + j * lp + (W - j) * lq
              for j in range(c, W + 1)]
    m = max(termos)
    return math.exp(m) * sum(math.exp(t - m) for t in termos)


def corte_apt(W, h, alfa_log2=ALFA_LOG2):
    """SP 800-90B 4.4.2: C = 1 + CRITBINOM(W, 2^-h, 1 - alfa)."""
    p = 2.0 ** -h
    alfa = 2.0 ** -alfa_log2
    if p >= 1.0:
        return W + 1
    c = max(1, int(W * p))
    while cauda_binomial(W, p, c) > alfa:
        c += 1
    return c


class TesteRepeticao:
    """Repetition Count Test (SP 800-90B 4.4.1): detecta valor 'grudado'."""

    def __init__(self, corte):
        self.corte, self.ultimo, self.run = corte, None, 0

    def reiniciar(self):
        self.ultimo, self.run = None, 0

    def verificar(self, s):
        if len(s) == 0:
            return True
        mud = np.flatnonzero(s[1:] != s[:-1]) + 1
        comp = np.diff(np.concatenate(([0], mud, [len(s)])))
        if self.ultimo is not None and s[0] == self.ultimo:
            comp[0] += self.run
        self.ultimo, self.run = s[-1], int(comp[-1])
        return int(comp.max()) < self.corte


class TesteProporcao:
    """Adaptive Proportion Test (SP 800-90B 4.4.2): detecta perda de entropia."""

    def __init__(self, W, corte):
        self.W, self.corte = W, corte
        self.resto = np.empty(0, dtype=np.uint8)

    def reiniciar(self):
        self.resto = self.resto[:0]

    def verificar(self, s):
        x = np.concatenate((self.resto, s))
        m = len(x) // self.W
        self.resto = x[m * self.W:].copy()
        if m == 0:
            return True
        jan = x[:m * self.W].reshape(m, self.W)
        return int((jan == jan[:, :1]).sum(axis=1).max()) < self.corte


def conferir_info(cfg, caminho):
    if not os.path.exists(caminho):
        print(f"  aviso: {caminho} nao encontrado; nao da para conferir se a fonte e a mesma "
              f"que foi avaliada no passo 'bruto'.")
        return
    with open(caminho, encoding="utf-8") as f:
        avaliada = json.load(f).get("fonte", {})
    atual = campos_fonte(cfg)
    dif = [("exposicao/ganho" if k == "props" else k) for k in atual if avaliada.get(k) != atual[k]]
    if dif:
        print(f"  ATENCAO: {', '.join(dif)} mudaram desde a avaliacao ({caminho}). O h medido nao\n"
              f"  vale para a fonte atual: refaca 'bruto' e o ea_non_iid.")


def cmd_gerar(a):
    cfg = carregar_config(a.config)
    amo = Amostrador(cfg)
    k, h = amo.bits, a.h
    if not 0 < h <= k:
        sys.exit(f"--h deve ficar entre 0 e {k}: e a min-entropia por amostra medida pelo ea_non_iid.")
    conferir_info(cfg, a.info)

    n_s = math.ceil(a.margem * 256 / h)              # amostras por entrada do SHA-256
    if k == 1:
        n_s = -(-n_s // 8) * 8                       # multiplo de 8 para empacotar em bytes
    n_in = n_s if k == 1 else 8 * n_s                # bits que entram no SHA-256
    h_in = n_s * h
    h_out = entropia_saida_vetada(n_in, 256, 256, h_in)
    corte_rct = 1 + math.ceil(ALFA_LOG2 / h)
    W = 1024 if k == 1 else 512
    c_apt = corte_apt(W, h)
    alvo = a.sequencias * a.n // 8

    print("Condicionamento: SHA-256 (componente aprovado, SP 800-90B 3.1.5.1)")
    print(f"  amostras por saida:      {fmt(n_s)}  (n_in = {fmt(n_in)} bits)")
    print(f"  min-entropia na entrada: h_in = {fmt(n_s)} x {h:g} = {h_in:.1f} bits")
    print(f"  min-entropia na saida:   h_out = {h_out:.4f} de 256 bits")
    print(f"     (confira:  ./ea_conditioning -v {n_in} 256 256 {h_in:.6f})")
    print(f"Testes de saude (alarme falso 2^-{ALFA_LOG2}): RCT corte {corte_rct} | "
          f"APT janela {W}, corte {c_apt}")
    print(f"Gerando {fmt(alvo * 8)} bits ({a.sequencias} sequencias de {fmt(a.n)}) em {a.saida}...")

    rct, apt = TesteRepeticao(corte_rct), TesteProporcao(W, c_apt)
    cap = abrir_camera(cfg)
    descrever_camera(cap)
    buf = np.empty(0, dtype=np.uint8)
    escritos = usados = repetidos = falhas = seguidas = 0
    ant, t0 = None, time.time()
    with open(a.saida, "wb") as f:
        while escritos < alvo:
            quadro = ler_quadro(cap)
            pix = amo.pixels(quadro, "h")
            if ant is not None and np.array_equal(pix, ant):
                repetidos += 1
                continue
            ant = pix
            s = amo.amostras(pix)
            ok_rct, ok_apt = rct.verificar(s), apt.verificar(s)
            if not (ok_rct and ok_apt):
                falhas += 1
                seguidas += 1
                buf = buf[:0]
                rct.reiniciar()
                apt.reiniciar()
                qual = " e ".join(nome for nome, ok in (("RCT", ok_rct), ("APT", ok_apt)) if not ok)
                print(f"\n  ALERTA: teste de saude {qual} falhou; amostras do quadro descartadas.")
                if seguidas >= 5:
                    sys.exit("Falhas seguidas: a fonte degradou (saturacao? lente tampada? "
                             "exposicao errada?). Revise com 'calibrar'.")
                continue
            seguidas = 0
            usados += 1
            buf = np.concatenate((buf, s))
            m = len(buf) // n_s
            if m:
                bloco, buf = buf[:m * n_s], buf[m * n_s:]
                dados = np.packbits(bloco).tobytes() if k == 1 else bloco.tobytes()
                tam = len(dados) // m
                saida = b"".join(hashlib.sha256(dados[i * tam:(i + 1) * tam]).digest() for i in range(m))
                saida = saida[:alvo - escritos]
                f.write(saida)
                escritos += len(saida)
            dt = max(1e-9, time.time() - t0)
            print(f"\r  {100 * escritos / alvo:5.1f}%  {escritos * 8 / 1e6:7.1f} Mbit  "
                  f"{escritos * 8 / dt / 1e3:7.1f} kbit/s  quadros {usados}  repetidos {repetidos}  "
                  f"falhas {falhas} ", end="", flush=True)
    cap.release()
    print(f"\nPronto: {a.saida} ({fmt(escritos)} bytes) em {(time.time() - t0) / 60:.1f} min. "
          f"Falhas de saude: {falhas}.")
    print(f"Proximo passo:  python trng_lava.py testar {a.saida}   (e o sts-2.1.2 oficial)")


# =============================================================================
# 5) testar - SP 800-22: Monobit, Frequencia em Blocos e Runs + 2o nivel
# =============================================================================

def igamc(a, x):
    """Funcao gama incompleta superior regularizada Q(a, x) (a 'igamc' do NIST STS)."""
    if x <= 0:
        return 1.0
    lg = a * math.log(x) - x - math.lgamma(a)
    if x < a + 1.0:                                   # serie para P(a, x)
        termo = soma = 1.0 / a
        ap = a
        for _ in range(100000):
            ap += 1.0
            termo *= x / ap
            soma += termo
            if abs(termo) < abs(soma) * 1e-16:
                break
        return max(0.0, 1.0 - soma * math.exp(lg))
    tiny = 1e-300                                     # fracao continua (Lentz) para Q(a, x)
    b = x + 1.0 - a
    c, d = 1.0 / tiny, 1.0 / b
    h = d
    for i in range(1, 100000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-16:
            break
    return min(1.0, math.exp(lg) * h)


def p_monobit(b):
    n = len(b)
    s = 2 * int(np.count_nonzero(b)) - n
    return math.erfc(abs(s) / math.sqrt(n) / math.sqrt(2))


def p_blocos(b, M):
    N = len(b) // M
    if N == 0:
        return float("nan")
    pi = b[:N * M].reshape(N, M).sum(axis=1, dtype=np.int64) / M
    chi2 = 4.0 * M * float(((pi - 0.5) ** 2).sum())
    return igamc(N / 2.0, chi2 / 2.0)


def p_runs(b):
    n = len(b)
    pi = np.count_nonzero(b) / n
    if abs(pi - 0.5) > 2.0 / math.sqrt(n):            # pre-requisito (igual ao STS)
        return 0.0
    v = 1 + int(np.count_nonzero(b[1:] != b[:-1]))
    return math.erfc(abs(v - 2.0 * n * pi * (1 - pi)) / (2.0 * pi * (1 - pi) * math.sqrt(2 * n)))


def segundo_nivel(pvals, alfa=0.01):
    """SP 800-22 secao 4.2, calculado exatamente como o computeMetrics() do STS 2.1.2."""
    m = len(pvals)
    aprovadas = sum(p >= alfa for p in pvals)
    p_hat = 1.0 - alfa
    minimo = int((p_hat - 3.0 * math.sqrt(p_hat * alfa / m)) * m)
    freq = [0] * 10
    for p in pvals:
        freq[min(int(math.floor(p * 10)), 9)] += 1
    esperado = m // 10
    unif = None
    if esperado:
        chi2 = sum((f - esperado) ** 2 / esperado for f in freq)
        unif = igamc(4.5, chi2 / 2.0)
    return freq, unif, aprovadas, minimo


EXEMPLO_NIST = ("11001001000011111101101010100010001000010110100011"
                "00001000110100110001001100011001100010100010111000")


def autoteste():
    b = np.frombuffer(EXEMPLO_NIST.encode(), dtype=np.uint8) - 48
    casos = [("Frequency (Monobit)", p_monobit(b), 0.109599, "2.1.8"),
             ("Block Frequency M=10", p_blocos(b, 10), 0.706438, "2.2.8"),
             ("Runs", p_runs(b), 0.500798, "2.3.8")]
    print("Autoteste com a sequencia de 100 bits dos exemplos do SP 800-22:")
    tudo_ok = True
    for nome, p, ref, secao in casos:
        ok = abs(p - ref) < 1e-6
        tudo_ok &= ok
        print(f"  {nome:<22} p = {p:.6f}   NIST (secao {secao}): {ref:.6f}   {'ok' if ok else 'DIFERENTE'}")
    print()
    return tudo_ok


def cmd_testar(a):
    if a.autoteste:
        autoteste()
        if not a.arquivo:
            return
    if not a.arquivo:
        sys.exit("Informe o arquivo:  python trng_lava.py testar saida_trng.bin")
    dados = np.fromfile(a.arquivo, dtype=np.uint8)
    bits = (dados & 1) if a.entrada == "bits" else np.unpackbits(dados)
    n = a.n
    m = min(a.m, len(bits) // n)
    if m == 0:
        sys.exit(f"{a.arquivo} tem {fmt(len(bits))} bits; cada sequencia precisa de {fmt(n)}.")
    testes = [("Frequency (Monobit)", p_monobit),
              (f"Block Frequency (M={a.M})", lambda b: p_blocos(b, a.M)),
              ("Runs", p_runs)]
    pv = {nome: [] for nome, _ in testes}
    for i in range(m):
        b = bits[i * n:(i + 1) * n]
        for nome, f in testes:
            pv[nome].append(f(b))
        print(f"\r  sequencia {i + 1}/{m}", end="", flush=True)
    print("\r" + " " * 30 + "\r", end="")

    print(f"Arquivo: {a.arquivo} | {m} sequencia(s) de {fmt(n)} bits | alfa = 0.01")
    linha = "-" * 96
    print(linha)
    print(" C1  C2  C3  C4  C5  C6  C7  C8  C9 C10   P-VALOR  PROPORCAO  TESTE")
    print(linha)
    reprovado = False
    for nome, _ in testes:
        freq, unif, aprov, minimo = segundo_nivel(pv[nome])
        ok_prop = aprov >= minimo if m >= 10 else aprov == m
        ok_unif = unif is None or unif >= 0.0001
        reprovado |= not (ok_prop and ok_unif)
        u = "    ----  " if unif is None else f"{unif:9.6f}{'*' if not ok_unif else ' '}"
        veredito = "ok" if (ok_prop and ok_unif) else \
            "REPROVADO (" + ", ".join(x for x, ok in (("proporcao", ok_prop), ("uniformidade", ok_unif)) if not ok) + ")"
        print(" " + " ".join(f"{c:3d}" for c in freq) + f" {u} {aprov:4d}/{m:<4d}{'*' if not ok_prop else ' '}  "
              f"{nome:<27} {veredito}")
    print(linha)
    _, _, _, minimo = segundo_nivel([1.0] * m)
    if m < 10:
        print(f"Com {m} sequencia(s) o criterio e p >= 0.01 em todas. Para a analise de 2o nivel "
              f"(proporcao e uniformidade) use 100 sequencias.")
    else:
        print(f"Criterios (SP 800-22 secao 4.2, como no relatorio do STS): pelo menos {minimo} de {m} "
              f"sequencias com p >= 0.01")
        if m >= 55:
            print("e P-VALOR de uniformidade >= 0.0001 (distribuicao dos p-valores nas faixas C1..C10).")
        else:
            print(f"Uniformidade: o SP 800-22 pede pelo menos 55 sequencias; aqui ha {m}.")
    if m <= 5:
        for nome, _ in testes:
            print(f"  {nome:<27} p-valores: " + ", ".join(f"{p:.6f}" for p in pv[nome]))
    print("\nResultado geral (3 testes):", "REPROVADO" if reprovado else "aprovado")


# =============================================================================
# Linha de comando
# =============================================================================

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="comando", metavar="comando")

    def com_config(sp):
        sp.add_argument("--config", default="config.json", help="arquivo de configuracao (padrao: config.json)")
        return sp

    c = com_config(sub.add_parser("calibrar", help="marcar as lampadas e ajustar a camera"))
    c.add_argument("--camera", type=int, help="indice da camera (0, 1, 2...)")
    c.add_argument("--backend", choices=["auto", "dshow", "msmf", "v4l2", "avfoundation", "any"])
    c.add_argument("--largura", type=int)
    c.add_argument("--altura", type=int)
    c.add_argument("--passo", type=int, help="usar 1 de cada N pixels nas ROIs (padrao 1)")
    c.add_argument("--bits-por-amostra", dest="bits_por_amostra", type=int,
                   help="bits menos significativos por pixel (padrao 1)")
    c.set_defaults(func=cmd_calibrar)

    c = com_config(sub.add_parser("bruto", help="gravar dados brutos para o SP 800-90B"))
    c.add_argument("--amostras", type=int, default=1_000_000, help="amostras por arquivo (padrao 1.000.000)")
    c.add_argument("--quadros", type=int, default=1000, help="quadros no arquivo temporal (padrao 1000)")
    c.add_argument("--posicoes", type=int, default=1000, help="pixels fixos no arquivo temporal (padrao 1000)")
    c.add_argument("--prefixo", default="bruto", help="prefixo dos arquivos (ex.: controle_tampada)")
    c.set_defaults(func=cmd_bruto)

    c = com_config(sub.add_parser("reinicio", help="gravar dados de reinicio para o SP 800-90B"))
    c.add_argument("--reinicios", type=int, default=1000)
    c.add_argument("--amostras", type=int, default=1000)
    c.add_argument("--saida", default="bruto_reinicio.bin")
    c.add_argument("--pausa", type=float, default=0.2, help="segundos entre fechar e reabrir (padrao 0.2)")
    c.add_argument("--do-zero", dest="do_zero", action="store_true", help="apaga o arquivo e recomeca")
    c.set_defaults(func=cmd_reinicio)

    c = com_config(sub.add_parser("gerar", help="gerar a saida condicionada (SHA-256)"))
    c.add_argument("--h", type=float, required=True, help="min-entropia por amostra medida pelo ea_non_iid")
    c.add_argument("--margem", type=float, default=2.0,
                   help="entropia de entrada por saida = margem x 256 bits (padrao 2)")
    c.add_argument("--sequencias", type=int, default=100, help="sequencias de --n bits (padrao 100)")
    c.add_argument("--n", type=int, default=1_000_000, help="bits por sequencia (padrao 1.000.000)")
    c.add_argument("--saida", default="saida_trng.bin")
    c.add_argument("--info", default="bruto_info.json", help="info gravada pelo passo 'bruto'")
    c.set_defaults(func=cmd_gerar)

    c = sub.add_parser("testar", help="Monobit, Frequencia em Blocos e Runs (SP 800-22)")
    c.add_argument("arquivo", nargs="?")
    c.add_argument("--n", type=int, default=1_000_000, help="bits por sequencia (padrao 1.000.000)")
    c.add_argument("--m", type=int, default=100, help="numero de sequencias (padrao 100)")
    c.add_argument("--M", type=int, default=128, help="bloco do Block Frequency (padrao do STS: 128)")
    c.add_argument("--entrada", choices=["bytes", "bits"], default="bytes",
                   help="bytes = 8 bits por byte (padrao); bits = 1 bit por byte (arquivos 'bruto')")
    c.add_argument("--autoteste", action="store_true", help="conferir com os exemplos do SP 800-22")
    c.set_defaults(func=cmd_testar)

    a = p.parse_args(argv)
    if not getattr(a, "func", None):
        p.print_help()
        return
    a.func(a)


if __name__ == "__main__":
    main()
