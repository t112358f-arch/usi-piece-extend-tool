#!/usr/bin/env python3
"""
USIエンジンプロキシ
GUIとUSIエンジンの間に入り、

  position [startpos|sfen ...] [moves ...]

を受け取ったとき、moves を全て適用した局面を SFEN に変換し、
持ち駒が上限を超えていればクランプして、

  position sfen <最終局面SFEN>          ← moves なし

としてエンジンへ送る。それ以外のコマンドはそのままパススルー。

上限値:
  飛・角          : 4個以上 → 3個
  金・銀・桂・香  : 8個以上 → 7個
  歩              : 32個以上 → 31個
"""

import sys
import os
import subprocess
import threading
import re

# ──────────────────────────────────────────────────────────────
# 持ち駒クランプ設定
# ──────────────────────────────────────────────────────────────
CAP = {'R': 3, 'B': 3, 'G': 7, 'S': 7, 'N': 7, 'L': 7, 'P': 31,
       'r': 3, 'b': 3, 'g': 7, 's': 7, 'n': 7, 'l': 7, 'p': 31}

# ──────────────────────────────────────────────────────────────
# 駒の定数
# ──────────────────────────────────────────────────────────────
EMPTY = 0
B_FU,  B_KY,  B_KE,  B_GI,  B_KA,  B_HI,  B_KI,  B_OU  = 1,2,3,4,5,6,7,8
B_TO,  B_NY,  B_NK,  B_NG,  B_UM,  B_RY               = 11,12,13,14,15,16
W_FU,  W_KY,  W_KE,  W_GI,  W_KA,  W_HI,  W_KI,  W_OU  = -1,-2,-3,-4,-5,-6,-7,-8
W_TO,  W_NY,  W_NK,  W_NG,  W_UM,  W_RY               = -11,-12,-13,-14,-15,-16

SFEN_TO_PIECE = {
    'P': B_FU, 'L': B_KY, 'N': B_KE, 'S': B_GI,
    'B': B_KA, 'R': B_HI, 'G': B_KI, 'K': B_OU,
    'p': W_FU, 'l': W_KY, 'n': W_KE, 's': W_GI,
    'b': W_KA, 'r': W_HI, 'g': W_KI, 'k': W_OU,
}
PIECE_TO_SFEN = {v: k for k, v in SFEN_TO_PIECE.items()}
PIECE_TO_SFEN.update({
    B_TO: '+P', B_NY: '+L', B_NK: '+N', B_NG: '+S', B_UM: '+B', B_RY: '+R',
    W_TO: '+p', W_NY: '+l', W_NK: '+n', W_NG: '+s', W_UM: '+b', W_RY: '+r',
})

HAND_CHAR_TO_PIECE = {
    'R': B_HI, 'B': B_KA, 'G': B_KI, 'S': B_GI,
    'N': B_KE, 'L': B_KY, 'P': B_FU,
    'r': B_HI, 'b': B_KA, 'g': B_KI, 's': B_GI,
    'n': B_KE, 'l': B_KY, 'p': B_FU,
}

PIECE_TO_HAND_SFEN_B = {B_HI:'R', B_KA:'B', B_KI:'G', B_GI:'S',
                         B_KE:'N', B_KY:'L', B_FU:'P'}
PIECE_TO_HAND_SFEN_W = {B_HI:'r', B_KA:'b', B_KI:'g', B_GI:'s',
                         B_KE:'n', B_KY:'l', B_FU:'p'}

PROMOTE = {
    B_FU:B_TO, B_KY:B_NY, B_KE:B_NK, B_GI:B_NG, B_KA:B_UM, B_HI:B_RY,
    W_FU:W_TO, W_KY:W_NY, W_KE:W_NK, W_GI:W_NG, W_KA:W_UM, W_HI:W_RY,
}
DEMOTE = {v: k for k, v in PROMOTE.items()}

def raw_piece(p):
    return DEMOTE.get(p, p)

# ──────────────────────────────────────────────────────────────
# 初期局面 SFEN
# ──────────────────────────────────────────────────────────────
HIRATE_SFEN = 'lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL b - 1'

# ──────────────────────────────────────────────────────────────
# Board クラス
# ──────────────────────────────────────────────────────────────
class Board:
    """
    board[row][col]  row=0が1段目(後手陣奥)、row=8が9段目(先手陣奥)
                     col=0が9筋、col=8が1筋
    hand[1]  先手持ち駒  {生駒ID: 枚数}
    hand[-1] 後手持ち駒
    turn: 1=先手、-1=後手
    """

    def __init__(self):
        self.board = [[EMPTY]*9 for _ in range(9)]
        self.hand  = {1: {}, -1: {}}
        self.turn  = 1
        self.move_count = 1

    @classmethod
    def from_sfen(cls, sfen: str) -> 'Board':
        b = cls()
        parts = sfen.strip().split()
        if len(parts) < 3:
            raise ValueError(f'SFEN短すぎ: {sfen!r}')
        board_str, turn_str, hand_str = parts[0], parts[1], parts[2]
        b.move_count = int(parts[3]) if len(parts) >= 4 else 1

        # 盤面パース
        rows = board_str.split('/')
        for row_i, row_s in enumerate(rows):
            col_i = 0
            promoted = False
            for ch in row_s:
                if ch == '+':
                    promoted = True
                    continue
                if ch.isdigit():
                    col_i += int(ch)
                    continue
                piece = SFEN_TO_PIECE[ch]
                if promoted:
                    piece = PROMOTE.get(piece, piece)
                    promoted = False
                b.board[row_i][col_i] = piece
                col_i += 1

        b.turn = 1 if turn_str == 'b' else -1

        # 持ち駒パース
        if hand_str != '-':
            for num_s, ch in re.findall(r'(\d*)([RBGSNLPrbgsnlp])', hand_str):
                cnt  = int(num_s) if num_s else 1
                raw  = HAND_CHAR_TO_PIECE[ch]
                side = 1 if ch.isupper() else -1
                b.hand[side][raw] = b.hand[side].get(raw, 0) + cnt

        return b

    def apply_move(self, move: str):
        """USI形式の1手を適用する。"""
        side = self.turn

        if move[1] == '*':
            # 駒打ち  例: P*5e
            piece_ch = move[0].upper()
            to_col   = 9 - int(move[2])
            to_row   = ord(move[3]) - ord('a')
            raw = HAND_CHAR_TO_PIECE[piece_ch]
            piece = raw if side == 1 else -raw
            self.board[to_row][to_col] = piece
            self.hand[side][raw] -= 1
            if self.hand[side][raw] == 0:
                del self.hand[side][raw]
        else:
            promote  = move.endswith('+')
            from_col = 9 - int(move[0])
            from_row = ord(move[1]) - ord('a')
            to_col   = 9 - int(move[2])
            to_row   = ord(move[3]) - ord('a')

            piece    = self.board[from_row][from_col]
            captured = self.board[to_row][to_col]

            if captured != EMPTY:
                # 成り駒は生に戻してから絶対値で先手基準の生駒IDを得る
                raw_cap = abs(raw_piece(captured))
                self.hand[side][raw_cap] = self.hand[side].get(raw_cap, 0) + 1

            if promote:
                piece = PROMOTE.get(piece, piece)
            self.board[to_row][to_col]    = piece
            self.board[from_row][from_col] = EMPTY

        self.turn = -side
        self.move_count += 1

    def to_sfen(self) -> str:
        # 盤面
        rows = []
        for row in self.board:
            s = ''
            empty_cnt = 0
            for piece in row:
                if piece == EMPTY:
                    empty_cnt += 1
                else:
                    if empty_cnt:
                        s += str(empty_cnt)
                        empty_cnt = 0
                    s += PIECE_TO_SFEN[piece]
            if empty_cnt:
                s += str(empty_cnt)
            rows.append(s)
        board_str = '/'.join(rows)

        turn_str = 'b' if self.turn == 1 else 'w'

        # 持ち駒: USI規定順 飛角金銀桂香歩、先手→後手
        ORDER = [B_HI, B_KA, B_KI, B_GI, B_KE, B_KY, B_FU]
        hand_str = ''
        for raw in ORDER:
            cnt = self.hand[1].get(raw, 0)
            if cnt:
                hand_str += ('' if cnt == 1 else str(cnt)) + PIECE_TO_HAND_SFEN_B[raw]
        for raw in ORDER:
            cnt = self.hand[-1].get(raw, 0)
            if cnt:
                hand_str += ('' if cnt == 1 else str(cnt)) + PIECE_TO_HAND_SFEN_W[raw]
        if not hand_str:
            hand_str = '-'

        return f'{board_str} {turn_str} {hand_str} {self.move_count}'


# ──────────────────────────────────────────────────────────────
# 持ち駒クランプ
# ──────────────────────────────────────────────────────────────
def clamp_hand(hand_str: str) -> str:
    if hand_str == '-':
        return hand_str
    result = []
    for num_s, ch in re.findall(r'(\d*)([RBGSNLPrbgsnlp])', hand_str):
        cnt     = int(num_s) if num_s else 1
        clamped = min(cnt, CAP[ch])
        if clamped == 1:
            result.append(ch)
        elif clamped > 1:
            result.append(f'{clamped}{ch}')
    return ''.join(result) if result else '-'


def clamp_sfen(sfen: str) -> str:
    parts = sfen.split(' ')
    if len(parts) >= 3:
        parts[2] = clamp_hand(parts[2])
    return ' '.join(parts)


# ──────────────────────────────────────────────────────────────
# position コマンド処理
# ──────────────────────────────────────────────────────────────
def process_position_command(line: str) -> str:
    """
    moves を全て適用した最終局面の SFEN に変換し、
    'position sfen <SFEN>' (moves なし) を返す。
    """
    tokens = line.split()
    if len(tokens) < 2:
        return line

    try:
        if tokens[1] == 'startpos':
            board = Board.from_sfen(HIRATE_SFEN)
            rest  = tokens[2:]
        elif tokens[1] == 'sfen':
            if len(tokens) < 6:
                return line
            sfen_str = ' '.join(tokens[2:6])
            board    = Board.from_sfen(sfen_str)
            rest     = tokens[6:]
        else:
            return line

        # moves を収集して適用
        in_moves = False
        for t in rest:
            if t == 'moves':
                in_moves = True
                continue
            if in_moves:
                board.apply_move(t)

        final_sfen = clamp_sfen(board.to_sfen())
        return f'position sfen {final_sfen}'

    except Exception as e:
        sys.stderr.write(f'[proxy] position 処理エラー: {e}  元コマンド: {line!r}\n')
        return line


# ──────────────────────────────────────────────────────────────
# エンジン I/O スレッド
# ──────────────────────────────────────────────────────────────
def engine_to_gui(engine_proc):
    try:
        for raw in engine_proc.stdout:
            sys.stdout.write(raw)
            sys.stdout.flush()
    except Exception:
        pass


def gui_to_engine(engine_proc):
    try:
        for raw in sys.stdin:
            line = raw.rstrip('\n')
            if line.startswith('position'):
                line = process_position_command(line)
            engine_proc.stdin.write(line + '\n')
            engine_proc.stdin.flush()
            if line.strip() == 'quit':
                break
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────
# メイン
# ──────────────────────────────────────────────────────────────
def main():
    path_file = os.path.join(os.getcwd(), 'engine_path.txt')
    try:
        with open(path_file, 'r', encoding='utf-8') as f:
            engine_path = f.read().strip()
    except FileNotFoundError:
        sys.stderr.write(f'[proxy] engine_path.txt が見つかりません: {path_file}\n')
        sys.exit(1)

    if not engine_path:
        sys.stderr.write('[proxy] engine_path.txt が空です\n')
        sys.exit(1)

    try:
        engine_proc = subprocess.Popen(
            [engine_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=os.path.dirname(os.path.abspath(engine_path)),
        )
    except Exception as e:
        sys.stderr.write(f'[proxy] エンジンの起動に失敗しました: {e}\n')
        sys.exit(1)

    t_out = threading.Thread(target=engine_to_gui, args=(engine_proc,), daemon=True)
    t_in  = threading.Thread(target=gui_to_engine, args=(engine_proc,), daemon=True)
    t_out.start()
    t_in.start()

    t_in.join()
    engine_proc.stdin.close()
    engine_proc.wait()


if __name__ == '__main__':
    main()
