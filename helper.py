#### JUN 9th 2026

import flame
import re
import os
from PySide6 import QtWidgets, QtCore, QtGui


class Project:
    def __init__(self):
        self.rd = os.getenv('VP_REPO_DIR')
        self.project = os.environ['VP_PROJECT']
        self.alias   = os.environ['VP_PROJECT_ALIAS']
        self.sequences = []
        self.PRJ_PATH = self.rd + '/' + self.project
        self.SHOTS_GLOBAL_PATH = self.PRJ_PATH + '/' + 'shots/'
        self.SEQ_PATH = {}
        self.SHOTS = {}
    
    def get_sequences(self):
        for fldr in os.listdir(self.SHOTS_GLOBAL_PATH):
            full_path = os.path.join(self.SHOTS_GLOBAL_PATH, fldr)
            self.sequences.append(fldr)
            
            if not os.path.isdir(full_path):
                self.sequences.remove(fldr)
    
    def get_sequences_path(self):
        self.get_sequences()
        for seq in self.sequences:
            sequence_path = self.SHOTS_GLOBAL_PATH + seq + '/'
            self.SEQ_PATH[seq] = sequence_path
    
    def get_shots(self):
        self.get_sequences_path()
        for seq in self.SEQ_PATH.values():
            try:
                shot_paths = os.listdir(seq)
            except Exception as e:
                print(e)
            shots_per_seq = []
            
            for path in shot_paths:
                shot_name = path.split('/')[-1]
                if 'sequence' in shot_name:
                    pass
                elif not any(char.isdigit() for char in shot_name):
                    pass
                else:
                    shots_per_seq.append(shot_name)
            try:
                sorted_shots_per_seq = sorted(shots_per_seq, key=lambda x: int(x.split('_')[-1]))
            except:
                print('Could not sort')
                sorted_shots_per_seq = shots_per_seq
            
            self.SHOTS[seq.split('/')[-2]] = sorted_shots_per_seq