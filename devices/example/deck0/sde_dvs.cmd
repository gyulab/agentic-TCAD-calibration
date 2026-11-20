; ============================================================================
; Sentaurus Structure Editor deck: bottom-gate IWO transistor (2D cross-section).
; Fixed device structure from the device specification (not editable by the agent).
; Lengths in um. Tokens @...@ are filled by the harness from the device geometry
; and the parameter card.
;   L_um         channel length = source/drain inner-edge separation
;   Lov_each_um  source/drain overlap with the gate, per side
;   W = 5 um enters through AreaFactor in the device deck
; ============================================================================

(sde:clear)
(sdegeo:set-default-boolean "ABA")

(define material_Si    "IWO")
(define material_Gox   "HfO2")
(define material_metal "Metal")

(define Tch_nm 3.5)
(define Tch_um 0.0035)
(define L_um @L_um@)
(define W_um 5)
(define Lov_total_um @Lov_total_um@)
(define Lov_each_um @Lov_each_um@)

(define x_total_left -0.25)
(define x_total_right @x_total_right@)
(define x_source_outer 0)
(define x_source_inner @x_source_inner@)
(define x_drain_inner @x_drain_inner@)
(define x_drain_outer @x_drain_outer@)

(define y_ch_bot 0)
(define y_ch_top 0.0035)

; IWO channel (3.5 nm), spanning both contacts and the channel.
(define V_Channel
  (sdegeo:create-rectangle
     (position x_source_outer y_ch_bot 0)
     (position x_drain_outer  y_ch_top 0)
     material_Si
     "Semiconductor"))

; HfO2 gate dielectric (7.0 nm) below the channel (bottom gate).
(define y_gox_top y_ch_bot)
(define Tgox_HFO2_um 7.000000e-03)
(define y_gox_bot (- y_gox_top Tgox_HFO2_um))

(define V_Gox1
  (sdegeo:create-rectangle
     (position x_source_outer y_gox_bot 0)
     (position x_drain_outer  y_gox_top 0)
     material_Gox
     "Gox1"))

; Gate metal under the full source overlap + channel + drain overlap.
(define y_gate_top y_gox_bot)
(define y_gate_bot (- y_gate_top 15e-3))

(define V_Gate1
  (sdegeo:create-rectangle
     (position x_source_outer y_gate_bot 0)
     (position x_drain_outer  y_gate_top 0)
     material_metal
     "Gate1"))

; Source / drain metal (Pd, 40 nm) on top of the channel.
(define y_sd_bot y_ch_top)
(define y_sd_top (+ y_ch_top 40e-3))

(define V_S
  (sdegeo:create-rectangle
     (position x_source_outer y_sd_bot 0)
     (position x_source_inner y_sd_top 0)
     material_metal
     "S"))

(define V_D
  (sdegeo:create-rectangle
     (position x_drain_inner y_sd_bot 0)
     (position x_drain_outer y_sd_top 0)
     material_metal
     "D"))

; Conformal HfO2 passivation (5.0 nm): channel top, contact tops, inner sidewalls.
(define Tpass_HFO2_um 5.000000e-03)
(define y_pass_channel_top (+ y_ch_top Tpass_HFO2_um))
(define y_pass_contact_top (+ y_sd_top Tpass_HFO2_um))
(define x_pass_si_inner    (+ x_source_inner Tpass_HFO2_um))
(define x_pass_di_inner    (- x_drain_inner  Tpass_HFO2_um))

(define V_HFO2_pass_channel_top
  (sdegeo:create-rectangle
     (position x_pass_si_inner y_ch_top 0)
     (position x_pass_di_inner y_pass_channel_top 0)
     material_Gox
     "HfO2_passivation_channel_top"))

(define V_HFO2_pass_source_top
  (sdegeo:create-rectangle
     (position x_source_outer y_sd_top 0)
     (position x_source_inner y_pass_contact_top 0)
     material_Gox
     "HfO2_passivation_source_top"))

(define V_HFO2_pass_drain_top
  (sdegeo:create-rectangle
     (position x_drain_inner y_sd_top 0)
     (position x_drain_outer y_pass_contact_top 0)
     material_Gox
     "HfO2_passivation_drain_top"))

(define V_HFO2_pass_source_inner_vertical_sidewall
  (sdegeo:create-rectangle
     (position x_source_inner y_ch_top 0)
     (position x_pass_si_inner y_pass_contact_top 0)
     material_Gox
     "HfO2_passivation_source_inner_vertical_sidewall"))

(define V_HFO2_pass_drain_inner_vertical_sidewall
  (sdegeo:create-rectangle
     (position x_pass_di_inner y_ch_top 0)
     (position x_drain_inner y_pass_contact_top 0)
     material_Gox
     "HfO2_passivation_drain_inner_vertical_sidewall"))

; Contacts: metal bodies are replaced by contacts on their boundary edges.
(define x_s_pick (* 0.5 (+ x_source_outer x_source_inner)))
(define x_d_pick (* 0.5 (+ x_drain_inner  x_drain_outer)))
(define y_sd_pick (* 0.5 (+ y_sd_bot y_sd_top)))
(define y_sd_top_y y_sd_top)
(define y_sd_bot_y y_sd_bot)

(define x_g_pick (* 0.5 (+ x_source_outer x_drain_outer)))
(define y_g_pick (* 0.5 (+ y_gate_bot y_gate_top)))

(sdegeo:define-contact-set "gate"   4 (color:rgb 1 0 0) "##")
(sdegeo:define-contact-set "source" 4 (color:rgb 1 0 0) "##")
(sdegeo:define-contact-set "drain"  4 (color:rgb 1 0 0) "##")

(sdegeo:set-contact (list (car (find-body-id (position x_g_pick y_g_pick 0)))) "gate")
(sdegeo:set-contact
  (list (car (find-edge-id (position x_g_pick y_gate_bot 0)))
        (car (find-edge-id (position x_g_pick y_gate_top 0))))
  "gate")
(sdegeo:delete-region (list (car (find-body-id (position x_g_pick y_g_pick 0)))))

(sdegeo:set-contact (list (car (find-body-id (position x_d_pick y_sd_pick 0)))) "drain")
(sdegeo:set-contact
  (list (car (find-edge-id (position x_d_pick y_sd_bot_y 0)))
        (car (find-edge-id (position x_d_pick y_sd_top_y 0))))
  "drain")
(sdegeo:delete-region (list (car (find-body-id (position x_d_pick y_sd_pick 0)))))

(sdegeo:set-contact (list (car (find-body-id (position x_s_pick y_sd_pick 0)))) "source")
(sdegeo:set-contact
  (list (car (find-edge-id (position x_s_pick y_sd_bot_y 0)))
        (car (find-edge-id (position x_s_pick y_sd_top_y 0))))
  "source")
(sdegeo:delete-region (list (car (find-body-id (position x_s_pick y_sd_pick 0)))))

; Uniform effective donor density of the IWO channel (free-carrier surrogate).
(define n_type_dop "PhosphorusActiveConcentration")
(define Nd_eff @Nd_eff@)

(sdepe:doping-constant-placement "Channel" n_type_dop Nd_eff "Semiconductor")

; Mesh: physics-aware graded mesh generated from config.MESH_SPECS. Graded interface
; refinement from both IWO/HfO2 interfaces and from the S/D contacts, lateral grading at the inner contact edges,
; charge-free gate dielectric coarse. The deck holds the production mesh spec.
; >>> harness mesh block (spec G2, scale 1)
(sdedr:define-refeval-window "RW_bg" "Rectangle"
  (position x_source_outer (+ y_pass_contact_top 5e-3) 0) (position x_drain_outer (- y_gate_bot 5e-3) 0))
(sdedr:define-refinement-size "RD_bg" 0.02 0.02 0.001 0.0005)
(sdedr:define-refinement-placement "RP_bg" "RD_bg" (list "window" "RW_bg"))
(sdedr:define-refinement-size "RD_ch" 0.01 0.00035 0.0005 0.0001)
(sdedr:define-refinement-function "RD_ch" "MaxLenInt" "IWO" "HfO2" 0.00015 1.4)
(sdedr:define-refinement-function "RD_ch" "MaxLenInt" "IWO" "Contact" 0.00015 1.4)
(sdedr:define-refinement-region "RP_ch" "RD_ch" "Semiconductor")
(sdedr:define-refinement-size "RD_ox" 0.01 0.002 0.001 0.0005)
(sdedr:define-refinement-region "RP_ox" "RD_ox" "Gox1")
(sdedr:define-refeval-window "RW_sL" "Rectangle"
  (position (- x_source_inner 0.1) (- y_gox_bot 0.001) 0) (position x_source_inner (+ y_ch_top 0.006) 0))
(sdedr:define-multibox-size "MB_sL" 0.01 0.00035 0.0003 0.0001 -1.25 0)
(sdedr:define-multibox-placement "MP_sL" "MB_sL" "RW_sL")
(sdedr:define-refeval-window "RW_sR" "Rectangle"
  (position x_source_inner (- y_gox_bot 0.001) 0) (position (+ x_source_inner 0.1) (+ y_ch_top 0.006) 0))
(sdedr:define-multibox-size "MB_sR" 0.01 0.00035 0.0003 0.0001 1.25 0)
(sdedr:define-multibox-placement "MP_sR" "MB_sR" "RW_sR")
(sdedr:define-refeval-window "RW_dL" "Rectangle"
  (position (- x_drain_inner 0.1) (- y_gox_bot 0.001) 0) (position x_drain_inner (+ y_ch_top 0.006) 0))
(sdedr:define-multibox-size "MB_dL" 0.01 0.00035 0.0003 0.0001 -1.25 0)
(sdedr:define-multibox-placement "MP_dL" "MB_dL" "RW_dL")
(sdedr:define-refeval-window "RW_dR" "Rectangle"
  (position x_drain_inner (- y_gox_bot 0.001) 0) (position (+ x_drain_inner 0.1) (+ y_ch_top 0.006) 0))
(sdedr:define-multibox-size "MB_dR" 0.01 0.00035 0.0003 0.0001 1.25 0)
(sdedr:define-multibox-placement "MP_dR" "MB_dR" "RW_dR")
; <<< harness mesh block

(sde:build-mesh "n1_msh")
