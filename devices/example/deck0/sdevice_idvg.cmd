* ============================================================================
* Sentaurus Device deck: bottom-gate IWO transistor, forward I_D-V_G sweep at
* fixed V_D. Tokens @...@ are filled by the harness:
*   @WF_gate@, @WF_sd@          parameter card (gate / source-drain work functions, eV)
*   @VD@                        drain bias of this run
*   @THREADS@, @INITSTEP@, @MINSTEP@, @MINSTEP_D@, @MAXSTEP@, @ITER@, @DIGITS@, @ERRREF@
*                               solver controls (harness numerical-recovery sequence)
* The gate sweep runs from -1.3 to 2.3 V (measured range -1.0 to 2.0 V plus 0.3 V on each side
* for the gate work-function alignment). CurrentPlot spacing 37.5 mV.
* The File, Electrode, Math and Solve sections are fixed by the harness.
* Band-gap narrowing off (the silicon OldSlotboom constants inherited from the IWO.par
* template are not evidence for IWO), spatial snapshots during the gate sweep at the
* steps of the 37.5 mV current-plot grid (21 V_G values, every 75 mV from -0.7 to 0.05 V around threshold),
* and a harness-owned CurrentPlot section appended at rendering.
* ============================================================================

File {
   Grid       = "n1_msh.tdr"
   Parameter  = "sdevice_iwo_process.par"
   Plot       = "n1_des.tdr"
   Current    = "n1_des.plt"
   Output     = "n1_des.log"
}

Electrode {
   { Name="gate"   Voltage=0 Workfunction=@WF_gate@ }
   { Name="source" Voltage=0 Schottky Workfunction=@WF_sd@
       eRecVelocity = 1.000e6 hRecVelocity = 1.000e6
       DistResist   = 1.000000e-07 }
   { Name="drain"  Voltage=0 Schottky Workfunction=@WF_sd@
       eRecVelocity = 1.000e6 hRecVelocity = 1.000e6
       DistResist   = 1.000000e-07 }
}

Physics {
   Temperature = 300
   AreaFactor  = 5
   Fermi
   EffectiveIntrinsicDensity(NoBandGapNarrowing)
}

Physics (Material="IWO") {
   Recombination (SRH
      (ElectricField(
          Lifetime          = Hurkx
          DensityCorrection = None)))
   eMultivalley(Nonparabolicity)
}

Plot {
   eDensity hDensity
   eCurrent hCurrent
   Totalcurrent
   ElectricField/Vector
   eQuasiFermi hQuasiFermi eQuasiFermiEnergy hQuasiFermiEnergy
   Potential Doping SpaceCharge
   eMobility hMobility
   DonorConcentration
   AcceptorConcentration
   eVelocity hVelocity
   conductionBandEnergy
   valenceBandEnergy
   BandGap
   EffectiveBandGap
   ElectronAffinity
   SRHRecombination
}

Math {
   Extrapolate
   RelErrControl
   Digits           = @DIGITS@
   Iterations       = @ITER@
   Number_of_threads = @THREADS@
   ExitOnFailure
   Notdamped        = 100
   Method           = Blocked
   SubMethod        = Super
   ACMethod         = Blocked
   ACSubMethod      = Super
   DirectCurrent
   ErrRef(Electron) = @ERRREF@
   ErrRef(Hole)     = @ERRREF@
   RhsMin           = 1e-20
   TrapDLN          = 1000
}

Solve {
   Coupled (LineSearchDamping=0.01) { Poisson }

   Coupled (LineSearchDamping=1e-4) { Poisson }
   Coupled { Poisson }
   Coupled { Poisson Electron }
   Coupled { Poisson Electron Hole }

   Quasistationary (
      DoZero
      InitialStep = @INITSTEP@ Increment = 1.35
      MinStep     = @MINSTEP_D@ MaxStep   = @MAXSTEP@
      Goal { Name="drain" Voltage=@VD@ }
   ) { Coupled { Poisson Electron Hole } }

   Quasistationary (
      InitialStep = @INITSTEP@ Increment = 1.2
      MinStep=@MINSTEP@ MaxStep=@MAXSTEP@
      Goal { Name="gate" Voltage=-1.30000 }
   ) { Coupled { Poisson Electron Hole } }

   save(FilePrefix = "n1_off")
   plugin { plot(FilePrefix = "n1_off") }

   NewCurrentPrefix = "iwo_IdVg_off"
   Quasistationary (
      DoZero
      InitialStep = @INITSTEP@ Increment = 1.2
      MinStep=@MINSTEP@ MaxStep=@MAXSTEP@
      Goal { Name="gate" Voltage=2.30000 }
   ) {
      Coupled { Poisson Electron Hole }
      CurrentPlot ( Time=(Range=(0 1) Intervals=96) )
      Plot ( FilePrefix = "n1_snap" Time=( 0; 0.08333333333; 0.1666666667; 0.1875; 0.2083333333; 0.2291666667; 0.25; 0.2708333333; 0.2916666667; 0.3125; 0.3333333333; 0.3541666667; 0.375; 0.4166666667; 0.5; 0.5833333333; 0.6666666667; 0.75; 0.8333333333; 0.9166666667; 1 ) NoOverwrite )
   }

   save(FilePrefix = "n1_on")
   plugin { plot(FilePrefix = "n1_on") }

}
